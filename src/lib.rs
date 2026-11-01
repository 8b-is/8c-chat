#![forbid(unsafe_code)]

pub mod chat;
mod icons;
pub mod json;
pub mod store;

use chat::Chat;
use json::Json;
use std::io::Read;
use std::sync::Mutex;

pub struct Request {
    pub method: String,
    pub path: String,
    pub query: String,
    pub token: String,
    pub body: String,
}

pub struct Response {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
}
fn response(status: u16, content_type: &'static str, body: impl Into<Vec<u8>>) -> Response {
    Response {
        status,
        content_type,
        body: body.into(),
    }
}
pub fn json_response(status: u16, body: Json) -> Response {
    response(status, "application/json; charset=utf-8", body.encode())
}
fn error(status: u16, text: &str) -> Response {
    json_response(status, obj! {"ok" => false, "error" => text})
}

/// Single-request HTTP/1 connection. Bounds headers/body and rejects ambiguous framing.
pub fn read_request(mut stream: impl Read) -> Result<Request, String> {
    let mut bytes = Vec::new();
    let split = loop {
        if let Some(at) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
            break at;
        }
        if bytes.len() >= 16_384 {
            return Err("Headers too large".into());
        }
        let mut buf = [0; 1024];
        let n = stream.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("Incomplete headers".into());
        }
        bytes.extend_from_slice(&buf[..n]);
    };
    if split > 16_380 {
        return Err("Headers too large".into());
    }
    let header = std::str::from_utf8(&bytes[..split]).map_err(|_| "Invalid headers")?;
    let mut lines = header.split("\r\n");
    let parts = lines
        .next()
        .ok_or("No request")?
        .split_whitespace()
        .collect::<Vec<_>>();
    if parts.len() != 3
        || !matches!(parts[2], "HTTP/1.1" | "HTTP/1.0")
        || !parts[1].starts_with('/')
        || parts[1].len() > 2048
    {
        return Err("Invalid request line".into());
    }
    let method = parts[0].to_string();
    let target = parts[1].to_string();
    let mut headers = std::collections::BTreeMap::new();
    for line in lines {
        let (name, value) = line.split_once(':').ok_or("Invalid header")?;
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err("Invalid header name".into());
        }
        if headers
            .insert(name.to_ascii_lowercase(), value.trim().to_string())
            .is_some()
        {
            return Err("Duplicate header".into());
        }
    }
    if headers.contains_key("transfer-encoding") {
        return Err("Chunked requests are not supported".into());
    }
    let length = match headers.get("content-length") {
        Some(s) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => {
            s.parse::<usize>().map_err(|_| "Invalid length")?
        }
        Some(_) => return Err("Invalid length".into()),
        None => 0,
    };
    if length > 65_536 {
        return Err("Request too large".into());
    }
    if method == "POST" {
        if !headers
            .get("content-type")
            .is_some_and(|s| s.split(';').next() == Some("application/json"))
        {
            return Err("Expected application/json".into());
        }
        if let Some(origin) = headers.get("origin") {
            let host = headers.get("host").ok_or("Missing host")?;
            if origin != &format!("https://{host}") && origin != &format!("http://{host}") {
                return Err("Origin mismatch".into());
            }
        }
    }
    let start = split + 4;
    while bytes.len() < start + length {
        let remaining = start + length - bytes.len();
        let mut buf = [0; 4096];
        let n = stream
            .read(&mut buf[..remaining.min(4096)])
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("Incomplete body".into());
        }
        bytes.extend_from_slice(&buf[..n]);
    }
    let body = std::str::from_utf8(&bytes[start..start + length])
        .map_err(|_| "Invalid UTF-8")?
        .to_string();
    let (path, query) = target.split_once('?').unwrap_or((&target, ""));
    Ok(Request {
        method,
        path: path.into(),
        query: query.into(),
        body,
        token: headers
            .get("authorization")
            .and_then(|s| s.strip_prefix("Bearer "))
            .unwrap_or("")
            .to_string(),
    })
}

pub fn dispatch(state: &Mutex<Chat>, req: &Request) -> Response {
    if matches!(req.method.as_str(), "GET" | "HEAD") {
        match req.path.as_str() {
            "/" => return response(200, "text/html; charset=utf-8", include_str!("page.html")),
            "/app.js" => {
                return response(
                    200,
                    "text/javascript; charset=utf-8",
                    include_str!("app.js"),
                )
            }
            "/favicon.svg" => return response(200, "image/svg+xml", icons::SVG),
            "/apple-touch-icon.png" => return response(200, "image/png", icons::apple_touch_png()),
            "/favicon.ico" => return response(200, "image/x-icon", icons::favicon_ico()),
            "/manifest.json" => return response(200, "application/manifest+json", icons::MANIFEST),
            "/api/health" => {
                return json_response(
                    200,
                    obj! {"ok" => true, "service" => "8c-chat", "ui_revision" => chat::REVISION},
                )
            }
            _ => (),
        }
    }
    let Ok(mut chat) = state.lock() else {
        return error(503, "The room is restarting. Your draft is safe.");
    };
    let result = if matches!(req.method.as_str(), "GET" | "HEAD") {
        match req.path.as_str() {
            "/api/config" => Ok(chat.config()),
            "/api/messages" => {
                // Room IDs contain only ASCII letters and hyphens. Tokens never enter URLs.
                let room = req
                    .query
                    .split('&')
                    .find_map(|p| p.strip_prefix("room="))
                    .unwrap_or("humayian-lounge");
                chat.room(room, &req.token)
            }
            _ => return error(404, "Not found"),
        }
    } else if req.method == "POST" {
        let body = match json::parse(&req.body) {
            Ok(value @ Json::Object(_)) => value,
            _ => return error(400, "Invalid JSON object"),
        };
        match req.path.as_str() {
            "/api/introduce" => chat.introduce(&body),
            "/api/profile" => chat.update_profile(&body),
            "/api/messages" => chat.post(&body),
            "/api/retry" => chat.retry(&body),
            "/api/hottub" => chat.set_tub(&body),
            "/api/worker/poll" => chat.poll(&body),
            "/api/worker/result" => chat.result(&body),
            "/api/harmony/handshake" => {
                return error(
                    409,
                    "Chat has been updated. Please refresh to introduce yourself.",
                )
            }
            _ => return error(404, "Not found"),
        }
    } else {
        return error(405, "Method not allowed");
    };
    match result {
        Ok(body) => json_response(200, body),
        Err(text) => error(
            if text.contains("authentication") {
                401
            } else if text.contains("could not save") {
                503
            } else {
                400
            },
            &text,
        ),
    }
}

pub fn write_http(method: &str, response: &Response) -> Vec<u8> {
    let reason = match response.status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        _ => "Service Unavailable",
    };
    let mut out = format!("HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'\r\n\r\n", response.status, reason, response.content_type, response.body.len()).into_bytes();
    if method != "HEAD" {
        out.extend_from_slice(&response.body);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_ambiguous_or_truncated_http() {
        for raw in [
            "POST / HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\nabc",
            "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            "POST / HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 4\r\n\r\n{}",
            "POST / HTTP/1.1\r\nHost: 8c.is\r\nOrigin: https://elsewhere.test\r\nContent-Type: application/json\r\n\r\n",
        ] { assert!(read_request(raw.as_bytes()).is_err()); }
        let raw = "POST /api/messages?room=hot-tub HTTP/1.1\r\nHost: 8c.is\r\nOrigin: https://8c.is\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}";
        assert_eq!(read_request(raw.as_bytes()).unwrap().body, "{}");
    }
}
