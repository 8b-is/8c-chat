#![forbid(unsafe_code)]

use eightc_chat::{chat::Chat, dispatch, json_response, obj, read_request, write_http};
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

struct DeadlineReader<'a> {
    stream: &'a mut std::net::TcpStream,
    deadline: Instant,
}
impl Read for DeadlineReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let remaining = self
            .deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "request deadline"))?;
        self.stream.set_read_timeout(Some(remaining))?;
        self.stream.read(buffer)
    }
}

fn run() -> Result<(), String> {
    let addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8798".into());
    let path =
        std::env::var("JOURNAL_PATH").unwrap_or_else(|_| "/var/lib/8c-chat/chat-v1.jsonl".into());
    let config = match std::env::var("AGENTS_CONFIG") {
        Ok(path) => std::fs::read_to_string(path).map_err(|e| e.to_string())?,
        Err(_) => include_str!("../agents.example.json").to_string(),
    };
    let key = std::env::var("WORKER_KEY").unwrap_or_default();
    let legacy = std::env::var("LEGACY_PATH").ok();
    let state = Arc::new(Mutex::new(Chat::open(
        Path::new(&path),
        &config,
        key,
        legacy.as_deref().map(Path::new),
    )?));
    let listener = TcpListener::bind(&addr).map_err(|e| e.to_string())?;
    let ticking = Arc::clone(&state);
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(3));
        if let Ok(mut chat) = ticking.lock() {
            if let Err(error) = chat.tick() {
                eprintln!("scheduler: {error}");
            }
        }
    });
    let (tx, rx) = mpsc::sync_channel::<std::net::TcpStream>(32);
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..16 {
        let state = Arc::clone(&state);
        let rx = Arc::clone(&rx);
        thread::spawn(move || loop {
            let stream = rx.lock().expect("receiver lock").recv();
            let Ok(mut stream) = stream else {
                break;
            };
            let _ = stream.set_read_timeout(Some(Duration::from_secs(8)));
            let _ = stream.set_write_timeout(Some(Duration::from_secs(8)));
            let (method, response) = match read_request(DeadlineReader {
                stream: &mut stream,
                deadline: Instant::now() + Duration::from_secs(8),
            }) {
                Ok(req) => (req.method.clone(), dispatch(&state, &req)),
                Err(_) => (
                    "GET".into(),
                    json_response(
                        400,
                        obj! {"ok" => false, "error" => "Invalid or incomplete HTTP request"},
                    ),
                ),
            };
            let _ = stream.write_all(&write_http(&method, &response));
        });
    }
    eprintln!(
        "8c-chat revision {} listening on {addr}",
        eightc_chat::chat::REVISION
    );
    for stream in listener.incoming().flatten() {
        if let Err(mpsc::TrySendError::Full(mut stream)) = tx.try_send(stream) {
            let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
            let _ = stream.write_all(&write_http(
                "GET",
                &json_response(
                    503,
                    obj! {"ok" => false, "error" => "Room busy; please retry. Your draft is safe."},
                ),
            ));
        }
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("8c-chat: {error}");
        std::process::exit(1);
    }
}
