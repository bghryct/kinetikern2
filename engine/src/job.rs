//! Asynchronous jobs: work runs on a thread the library owns, using a rayon
//! pool; the caller only polls atomics.
//!
//! The Glyphs plugin never blocks on the engine and never runs Python while
//! the engine works: it starts a job, polls `Progress` from a timer on the
//! main thread (a few loads of atomics), and takes the output once the state
//! says so. Nothing ever calls back into Python. Cancelling sets a flag that
//! every work item checks, so a running job stops within one pair's time.

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

/// Returned by work that noticed the cancel flag.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cancelled;

pub const STATE_RUNNING: u32 = 0;
pub const STATE_DONE: u32 = 1;
pub const STATE_FAILED: u32 = 2;
pub const STATE_CANCELLED: u32 = 3;

/// Progress of one job. `done / total` is the fraction of the current phase;
/// totals are estimated work (ray counts), not item counts, so the bar moves
/// evenly.
pub struct Progress {
    cancel: AtomicBool,
    pub state: AtomicU32,
    pub phase: AtomicU32,
    pub phases: AtomicU32,
    pub done: AtomicU64,
    pub total: AtomicU64,
    started: Instant,
}

impl Default for Progress {
    fn default() -> Self {
        Self::new()
    }
}

impl Progress {
    pub fn new() -> Self {
        Progress {
            cancel: AtomicBool::new(false),
            state: AtomicU32::new(STATE_RUNNING),
            phase: AtomicU32::new(0),
            phases: AtomicU32::new(0),
            done: AtomicU64::new(0),
            total: AtomicU64::new(0),
            started: Instant::now(),
        }
    }

    /// Starts phase `phase` of `phases` with `total` units of work.
    pub fn begin(&self, phase: u32, phases: u32, total: u64) {
        self.done.store(0, Relaxed);
        self.total.store(total.max(1), Relaxed);
        self.phases.store(phases, Relaxed);
        self.phase.store(phase, Relaxed);
    }

    #[inline]
    pub fn add(&self, n: u64) {
        if n > 0 {
            self.done.fetch_add(n, Relaxed);
        }
    }

    #[inline]
    pub fn cancelled(&self) -> bool {
        self.cancel.load(Relaxed)
    }

    pub fn cancel(&self) {
        self.cancel.store(true, Relaxed);
    }

    pub fn elapsed(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }
}

/// Logical CPUs of the host.
pub fn cpu_count() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4)
}

/// Threads used when the caller asks for "auto": all cores but one, which is
/// left to the application's main thread.
pub fn default_threads() -> usize {
    cpu_count().saturating_sub(1).max(1)
}

/// Runs the calling thread below the application's main thread. Threads
/// inherit the QoS class of the thread that creates them, and the jobs are
/// started from Glyphs' main thread (user-interactive): left there, seven
/// busy workers compete with the user interface for the cores. User-initiated
/// still runs on the performance cores, but the scheduler prefers the main
/// thread.
#[cfg(target_os = "macos")]
fn below_main_thread() {
    extern "C" {
        fn pthread_set_qos_class_self_np(qos_class: u32, relative_priority: i32) -> i32;
    }
    const QOS_CLASS_USER_INITIATED: u32 = 0x19;
    // SAFETY: changes only the calling thread's scheduling class; a failure
    // (an error code) leaves it as it was.
    unsafe {
        pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
    }
}

#[cfg(not(target_os = "macos"))]
fn below_main_thread() {}

/// The rayon pool for a thread count (0 = auto). Pools are built once per
/// count and shared by all jobs.
pub fn pool(threads: usize) -> Arc<rayon::ThreadPool> {
    static POOLS: OnceLock<Mutex<HashMap<usize, Arc<rayon::ThreadPool>>>> = OnceLock::new();
    let n = if threads == 0 { default_threads() } else { threads.min(256) };
    let mut map = POOLS.get_or_init(|| Mutex::new(HashMap::new())).lock().unwrap_or_else(|e| e.into_inner());
    map.entry(n)
        .or_insert_with(|| {
            Arc::new(
                rayon::ThreadPoolBuilder::new()
                    .num_threads(n)
                    .thread_name(|i| format!("kinetikern2-{i}"))
                    .start_handler(|_| below_main_thread())
                    .build()
                    .unwrap_or_else(|_| rayon::ThreadPoolBuilder::new().num_threads(1).build().expect("rayon pool")),
            )
        })
        .clone()
}

/// What a finished job hands back.
pub enum Output<T> {
    Value(T),
    Error(String),
}

/// A job shared between the caller's handle and the worker thread.
pub struct JobShared<T> {
    pub progress: Progress,
    output: Mutex<Option<Output<T>>>,
}

pub struct Job<T> {
    pub shared: Arc<JobShared<T>>,
}

impl<T: Send + 'static> Job<T> {
    /// Runs `work` on a new thread. Panics become errors.
    pub fn spawn(name: &str, work: impl FnOnce(&Progress) -> Result<T, JobError> + Send + 'static) -> Self {
        let shared = Arc::new(JobShared { progress: Progress::new(), output: Mutex::new(None) });
        let worker = shared.clone();
        let spawned = std::thread::Builder::new().name(name.to_string()).spawn(move || {
            below_main_thread();
            let p = &worker.progress;
            let result = catch_unwind(AssertUnwindSafe(|| work(p)));
            let (state, out) = match result {
                Ok(Ok(v)) => (STATE_DONE, Some(Output::Value(v))),
                Ok(Err(JobError::Cancelled)) => (STATE_CANCELLED, None),
                Ok(Err(JobError::Failed(msg))) => (STATE_FAILED, Some(Output::Error(msg))),
                Err(panic) => {
                    let msg = panic
                        .downcast_ref::<&str>()
                        .map(|s| s.to_string())
                        .or_else(|| panic.downcast_ref::<String>().cloned())
                        .unwrap_or_else(|| "unknown panic".into());
                    (STATE_FAILED, Some(Output::Error(format!("internal error in Kinetikern2 engine: {msg}"))))
                }
            };
            *worker.output.lock().unwrap_or_else(|e| e.into_inner()) = out;
            worker.progress.state.store(state, Relaxed);
        });
        if let Err(e) = spawned {
            *shared.output.lock().unwrap_or_else(|e| e.into_inner()) =
                Some(Output::Error(format!("could not start a worker thread: {e}")));
            shared.progress.state.store(STATE_FAILED, Relaxed);
        }
        Job { shared }
    }

    pub fn state(&self) -> u32 {
        self.shared.progress.state.load(Relaxed)
    }

    /// Takes the value of a finished job (once).
    pub fn take(&self) -> Option<T> {
        if self.state() != STATE_DONE {
            return None;
        }
        match self.shared.output.lock().unwrap_or_else(|e| e.into_inner()).take() {
            Some(Output::Value(v)) => Some(v),
            _ => None,
        }
    }

    pub fn error(&self) -> Option<String> {
        match &*self.shared.output.lock().unwrap_or_else(|e| e.into_inner()) {
            Some(Output::Error(m)) => Some(m.clone()),
            _ => None,
        }
    }

    /// Blocks until the job leaves the running state (tools and tests only;
    /// the plugin polls instead). Returns the state.
    pub fn wait(&self, timeout_s: f64) -> u32 {
        let t = Instant::now();
        loop {
            let st = self.state();
            if st != STATE_RUNNING || (timeout_s > 0.0 && t.elapsed().as_secs_f64() > timeout_s) {
                return st;
            }
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
    }
}

pub enum JobError {
    Cancelled,
    Failed(String),
}

impl From<Cancelled> for JobError {
    fn from(_: Cancelled) -> Self {
        JobError::Cancelled
    }
}
