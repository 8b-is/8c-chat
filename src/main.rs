#![forbid(unsafe_code)]

use eightc_chat::{
    complete_aye, dispatch, hottub_turn, read_request, write_http, Ask, State, TUB_PAUSE_SECS,
};
use std::io::Write;
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

fn main() {
    let addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8798".to_string());
    let path = std::env::var("STATE_PATH").unwrap_or_else(|_| "/var/lib/8c-chat/rooms.m8".to_string());
    let state = Arc::new(Mutex::new(State::load(&path)));
    let listener = TcpListener::bind(&addr).unwrap_or_else(|err| {
        eprintln!("8c-chat could not bind {addr}: {err}");
        std::process::exit(1);
    });
    eprintln!("8c-chat listening on {addr}");
    let tub_state = Arc::clone(&state);
    let tub_on = std::env::var("HOTTUB").ok().as_deref() != Some("0");
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(8));
        loop {
            if let Some(job) = hottub_turn(&tub_state, tub_on) {
                complete_aye(&tub_state, job);
            }
            thread::sleep(Duration::from_secs(TUB_PAUSE_SECS));
        }
    });
    for conn in listener.incoming() {
        let Ok(stream) = conn else {
            continue;
        };
        let state = Arc::clone(&state);
        thread::spawn(move || {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(8)));
            let _ = stream.set_write_timeout(Some(Duration::from_secs(8)));
            let mut stream = stream;
            let Ok(incoming) = read_request(&mut stream) else {
                let _ = stream.write_all(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
                return;
            };
            let outcome = dispatch(
                &state,
                &Ask {
                    method: &incoming.method,
                    path: &incoming.path,
                    query: &incoming.query,
                    body: &incoming.body,
                    host: &incoming.host,
                },
            );
            if let Some(job) = outcome.aye {
                let aye_state = Arc::clone(&state);
                thread::spawn(move || complete_aye(&aye_state, job));
            }
            let bytes = write_http(&incoming.method, &outcome.response);
            let _ = stream.write_all(&bytes);
        });
    }
}
