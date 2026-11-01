#![forbid(unsafe_code)]

//! 8c.is family room.
//!
//! A chosen name is a choice, not a biometric result. Empty requests are not Hue.
//! Location, keystroke, and voiceprint fields are not treated as proof.

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const UI_REVISION: u32 = 6;
pub const RING_CAP: usize = 64;
pub const TOKEN_CAP: usize = 64;
pub const MAX_TEXT: usize = 2000;
pub const PRESENCE_SECS: u64 = 180;
pub const RATE_WINDOW_SECS: u64 = 60;
pub const RATE_MAX: u32 = 20;

const PAGE: &str = include_str!("page.html");

static AYE_BUSY: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Debug)]
pub struct Person {
    pub handle: &'static str,
    pub name: &'static str,
    pub hz: u16,
    pub kind: &'static str,
}

#[derive(Clone, Debug)]
pub struct Message {
    pub id: u64,
    pub timestamp_secs: u64,
    pub room: String,
    pub handle: String,
    pub name: String,
    pub kind: String,
    pub platform: String,
    pub hz: u16,
    pub text: String,
}

#[derive(Clone, Debug)]
struct Token {
    token: String,
    handle: String,
}

#[derive(Clone, Debug)]
struct Presence {
    handle: String,
    name: String,
    room: String,
    platform: String,
    seen_secs: u64,
}

#[derive(Clone, Debug)]
struct Rate {
    key: String,
    window_start: u64,
    count: u32,
}

pub struct State {
    path: PathBuf,
    messages: Vec<Message>,
    tokens: Vec<Token>,
    presence: Vec<Presence>,
    rates: Vec<Rate>,
    next_id: u64,
    token_seq: u64,
}

#[derive(Clone, Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
    pub cache: &'static str,
    pub csp: bool,
}

#[derive(Clone, Debug)]
pub struct AyeJob {
    pub room: String,
    pub prompt: String,
    pub fallback: String,
}

#[derive(Clone, Debug)]
pub struct Outcome {
    pub response: HttpResponse,
    pub aye: Option<AyeJob>,
}

pub struct Ask<'a> {
    pub method: &'a str,
    pub path: &'a str,
    pub query: &'a str,
    pub body: &'a str,
    pub host: &'a str,
}

pub fn normalize_handle(raw: &str) -> Option<&'static str> {
    let s = raw.trim().to_ascii_lowercase();
    match s.as_str() {
        "dra" | "alexandra" | "dra@8c.is" | "dra@8b.is" => Some("dra@8c.is"),
        "hue" | "chris" | "hue@8c.is" | "c@8b.is" => Some("hue@8c.is"),
        "trish" | "trisha" | "trish@8c.is" => Some("trish@8c.is"),
        "peter" | "omni" | "peter@8c.is" => Some("peter@8c.is"),
        "guest" | "guest@8c.is" | "just visiting" => Some("guest@8c.is"),
        "aye" | "aye@8c.is" => Some("aye@8c.is"),
        _ => None,
    }
}

fn person_for(handle: &str) -> Option<Person> {
    match normalize_handle(handle)? {
        "dra@8c.is" => Some(Person {
            handle: "dra@8c.is",
            name: "Alexandra",
            hz: 528,
            kind: "human",
        }),
        "hue@8c.is" => Some(Person {
            handle: "hue@8c.is",
            name: "Hue",
            hz: 432,
            kind: "human",
        }),
        "trish@8c.is" => Some(Person {
            handle: "trish@8c.is",
            name: "Trish",
            hz: 0,
            kind: "human",
        }),
        "peter@8c.is" => Some(Person {
            handle: "peter@8c.is",
            name: "Peter",
            hz: 0,
            kind: "human",
        }),
        "guest@8c.is" => Some(Person {
            handle: "guest@8c.is",
            name: "Guest",
            hz: 0,
            kind: "guest",
        }),
        "aye@8c.is" => Some(Person {
            handle: "aye@8c.is",
            name: "Aye",
            hz: 528,
            kind: "aye",
        }),
        _ => None,
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn clean_label(raw: &str, max_chars: usize) -> String {
    let mut out = String::new();
    for ch in raw.chars() {
        if out.chars().count() >= max_chars {
            break;
        }
        if ch == '\t' || ch == '\n' || ch == '\r' || (ch as u32) < 0x20 {
            out.push(' ');
        } else {
            out.push(ch);
        }
    }
    out.trim().to_string()
}

fn valid_room(room: &str) -> bool {
    let bytes = room.as_bytes();
    (1..=32).contains(&bytes.len())
        && bytes
            .iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn keep_message(text: &str) -> bool {
    let text = text.trim();
    if text == "ping-probe-do-not-keep" {
        return false;
    }
    if text.contains("IMPOSSIBLE TRAVEL:") {
        return false;
    }
    true
}

impl State {
    pub fn load(path: impl AsRef<Path>) -> Self {
        let path = path.as_ref().to_path_buf();
        let mut state = Self {
            path,
            messages: Vec::new(),
            tokens: Vec::new(),
            presence: Vec::new(),
            rates: Vec::new(),
            next_id: 1,
            token_seq: 1,
        };
        let Ok(text) = fs::read_to_string(&state.path) else {
            return state;
        };
        let mut dirty = false;
        let mut max_id = 0u64;
        for line in text.lines() {
            if line.is_empty() {
                continue;
            }
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.first() == Some(&"VPTOK") && parts.len() >= 3 {
                state.tokens.push(Token {
                    token: parts[1].to_string(),
                    handle: parts[2].to_string(),
                });
                if let Some(n) = parts[1].rsplit('_').next().and_then(|s| s.parse::<u64>().ok()) {
                    if n >= state.token_seq {
                        state.token_seq = n.saturating_add(1);
                    }
                }
                continue;
            }
            if parts.first() != Some(&"MSG") || parts.len() < 10 {
                continue;
            }
            let Ok(id) = parts[1].parse::<u64>() else {
                continue;
            };
            let Ok(timestamp_secs) = parts[2].parse::<u64>() else {
                continue;
            };
            let text = if parts.len() > 10 && parts[parts.len() - 1].is_empty() {
                parts[9..parts.len() - 1].join("\t")
            } else {
                parts[9..].join("\t")
            };
            if !keep_message(&text) {
                dirty = true;
                if id > max_id {
                    max_id = id;
                }
                continue;
            }
            let hz = parts[8].parse::<u16>().unwrap_or(0);
            if id > max_id {
                max_id = id;
            }
            state.messages.push(Message {
                id,
                timestamp_secs,
                room: parts[3].to_string(),
                handle: parts[4].to_string(),
                name: parts[5].to_string(),
                kind: parts[6].to_string(),
                platform: parts[7].to_string(),
                hz,
                text,
            });
        }
        state.next_id = max_id.saturating_add(1).max(1);
        if dirty {
            let _ = state.save();
        }
        state
    }

    fn save(&self) -> Result<(), ()> {
        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent).map_err(|_| ())?;
            }
        }
        let mut body = String::new();
        for token in &self.tokens {
            body.push_str("VPTOK\t");
            body.push_str(&token.token);
            body.push('\t');
            body.push_str(&token.handle);
            body.push('\n');
        }
        for msg in &self.messages {
            body.push_str(&format!(
                "MSG\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t\n",
                msg.id,
                msg.timestamp_secs,
                msg.room,
                msg.handle,
                msg.name,
                msg.kind,
                msg.platform,
                msg.hz,
                msg.text
            ));
        }
        let tmp = self.path.with_extension("m8.tmp");
        fs::write(&tmp, body).map_err(|_| ())?;
        fs::rename(&tmp, &self.path).map_err(|_| ())?;
        Ok(())
    }

    fn issue_token(&mut self, handle: &str) -> String {
        self.token_seq = self.token_seq.saturating_add(1);
        let token = format!(
            "vp8c_{:016x}_{}",
            now_secs()
                .wrapping_mul(0x9E37_79B9_7F4A_7C15)
                .wrapping_add(self.token_seq.wrapping_mul(0x1000_0000_01B3)),
            self.token_seq
        );
        self.tokens.push(Token {
            token: token.clone(),
            handle: handle.to_string(),
        });
        if self.tokens.len() > TOKEN_CAP {
            let drop_n = self.tokens.len() - TOKEN_CAP;
            self.tokens.drain(0..drop_n);
        }
        token
    }

    fn token_handle(&self, token: &str) -> Option<&str> {
        if token.is_empty() {
            return None;
        }
        self.tokens
            .iter()
            .rev()
            .find(|t| t.token == token)
            .map(|t| t.handle.as_str())
    }

    fn touch(&mut self, handle: &str, name: &str, room: &str, platform: &str, now: u64) {
        if handle.is_empty() {
            return;
        }
        if let Some(found) = self.presence.iter_mut().find(|p| p.handle == handle) {
            found.name = name.to_string();
            found.room = room.to_string();
            found.platform = platform.to_string();
            found.seen_secs = now;
        } else {
            self.presence.push(Presence {
                handle: handle.to_string(),
                name: name.to_string(),
                room: room.to_string(),
                platform: platform.to_string(),
                seen_secs: now,
            });
        }
        self.presence.retain(|p| now.saturating_sub(p.seen_secs) <= PRESENCE_SECS);
    }

    fn allow_post(&mut self, key: &str, now: u64) -> bool {
        if let Some(rate) = self.rates.iter_mut().find(|r| r.key == key) {
            if now.saturating_sub(rate.window_start) >= RATE_WINDOW_SECS {
                rate.window_start = now;
                rate.count = 1;
                return true;
            }
            if rate.count >= RATE_MAX {
                return false;
            }
            rate.count = rate.count.saturating_add(1);
            return true;
        }
        self.rates.push(Rate {
            key: key.to_string(),
            window_start: now,
            count: 1,
        });
        true
    }

    fn push_message(&mut self, msg: Message) -> Result<(), ()> {
        let room = msg.room.clone();
        self.messages.push(msg);
        let mut room_ids: Vec<u64> = self
            .messages
            .iter()
            .filter(|m| m.room == room)
            .map(|m| m.id)
            .collect();
        if room_ids.len() > RING_CAP {
            room_ids.sort_unstable();
            let drop_until = room_ids.len() - RING_CAP;
            let doomed: Vec<u64> = room_ids.into_iter().take(drop_until).collect();
            self.messages.retain(|m| !doomed.contains(&m.id));
        }
        self.save()
    }
}

fn json_escape(out: &mut String, text: &str) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn json_string(body: &str, key: &str) -> Option<String> {
    let pattern = format!("\"{key}\"");
    let mut rest = body;
    while let Some(index) = rest.find(&pattern) {
        let after = rest[index + pattern.len()..].trim_start();
        if let Some(after) = after.strip_prefix(':') {
            return parse_json_string(after.trim_start());
        }
        rest = &rest[index + pattern.len()..];
    }
    None
}

fn parse_json_string(raw: &str) -> Option<String> {
    let bytes = raw.as_bytes();
    if bytes.first() != Some(&b'"') {
        return None;
    }
    let mut out = String::new();
    let mut i = 1usize;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'"' {
            return Some(out);
        }
        if b == b'\\' {
            i += 1;
            if i >= bytes.len() {
                return None;
            }
            match bytes[i] {
                b'"' => out.push('"'),
                b'\\' => out.push('\\'),
                b'/' => out.push('/'),
                b'n' => out.push('\n'),
                b'r' => out.push('\r'),
                b't' => out.push('\t'),
                b'u' => {
                    if i + 4 >= bytes.len() {
                        return None;
                    }
                    let hex = &raw[i + 1..i + 5];
                    let code = u32::from_str_radix(hex, 16).ok()?;
                    out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                    i += 4;
                }
                _ => return None,
            }
            i += 1;
            continue;
        }
        if b < 0x20 {
            return None;
        }
        let ch = raw[i..].chars().next()?;
        out.push(ch);
        i += ch.len_utf8();
    }
    None
}

fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&raw[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        if bytes[i] == b'+' {
            out.push(b' ');
        } else {
            out.push(bytes[i]);
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn query_get(query: &str, key: &str) -> Option<String> {
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let mut parts = pair.splitn(2, '=');
        let k = parts.next().unwrap_or("");
        let v = parts.next().unwrap_or("");
        if percent_decode(k) == key {
            return Some(percent_decode(v));
        }
    }
    None
}

fn domain_of(host: &str) -> String {
    let host = host.split(',').next().unwrap_or(host).trim();
    let host = host.split(':').next().unwrap_or(host).trim().to_ascii_lowercase();
    if host == "aihuman.is" || host.ends_with(".aihuman.is") {
        "aihuman.is".to_string()
    } else {
        "8c.is".to_string()
    }
}

fn response(status: u16, content_type: &'static str, body: impl Into<Vec<u8>>, csp: bool) -> HttpResponse {
    HttpResponse {
        status,
        content_type,
        body: body.into(),
        cache: "no-store",
        csp,
    }
}

fn json_response(status: u16, body: String) -> HttpResponse {
    response(status, "application/json; charset=utf-8", body, false)
}

fn greeting_for(person: Option<&Person>) -> &'static str {
    match person.map(|p| p.handle) {
        Some("dra@8c.is") => {
            "Hey Alexandra. This phone will remember you. Talk or type, either one is the real thing."
        }
        Some("hue@8c.is") => "Hey Hue. The room is open.",
        Some("trish@8c.is") => "Hey Trish. Glad you are here.",
        Some("peter@8c.is") => "Hey Peter. The room is open.",
        Some("guest@8c.is") => "You are visiting. The family can still hear this room.",
        _ => "Who is here? I will not guess.",
    }
}

fn handshake_json(state: &mut State, body: &str, host: &str) -> String {
    let claimed = json_string(body, "claimed_handle").unwrap_or_default();
    let person = person_for(&claimed);
    let chosen = person.is_some();
    let token = if let Some(person) = person.as_ref() {
        let token = state.issue_token(person.handle);
        let _ = state.save();
        token
    } else {
        String::new()
    };
    let handle = person.as_ref().map(|p| p.handle).unwrap_or("");
    let name = person.as_ref().map(|p| p.name).unwrap_or("");
    let hz = person.as_ref().map(|p| p.hz).unwrap_or(0);
    let mut out = String::from("{\"ok\":true,\"verified\":");
    out.push_str(if chosen { "true" } else { "false" });
    out.push_str(",\"impossible_travel\":false,\"recognition\":");
    json_escape(&mut out, if chosen { "chosen" } else { "none" });
    out.push_str(",\"recognized_handle\":");
    json_escape(&mut out, handle);
    out.push_str(",\"recognized_name\":");
    json_escape(&mut out, name);
    out.push_str(&format!(",\"carrier_hz\":{hz},\"confidence_pq\":\""));
    out.push_str(if chosen { "1/1" } else { "0/1" });
    out.push_str("\",\"factors_summary\":");
    json_escape(
        &mut out,
        if chosen {
            "Name chosen on this device. Not a voiceprint, and not a location."
        } else {
            "No name chosen."
        },
    );
    out.push_str(",\"aye_greeting\":");
    json_escape(&mut out, greeting_for(person.as_ref()));
    out.push_str(",\"vp_token\":");
    json_escape(&mut out, &token);
    out.push_str(",\"domain\":");
    json_escape(&mut out, &domain_of(host));
    out.push_str(",\"ui_revision\":");
    out.push_str(&UI_REVISION.to_string());
    out.push('}');
    out
}

fn message_json(out: &mut String, msg: &Message) {
    let trit: i32 = if msg.kind == "guest" { 0 } else { 1 };
    out.push_str("{\"id\":");
    out.push_str(&msg.id.to_string());
    out.push_str(",\"room\":");
    json_escape(out, &msg.room);
    out.push_str(",\"sender_handle\":");
    json_escape(out, &msg.handle);
    out.push_str(",\"sender_name\":");
    json_escape(out, &msg.name);
    out.push_str(",\"sender_kind\":");
    json_escape(out, &msg.kind);
    out.push_str(",\"platform\":");
    json_escape(out, &msg.platform);
    out.push_str(",\"text\":");
    json_escape(out, &msg.text);
    out.push_str(",\"audio_data_url\":\"\",\"carrier_hz\":");
    out.push_str(&msg.hz.to_string());
    out.push_str(",\"salience_pq\":\"");
    out.push_str(if msg.kind == "guest" { "0/1" } else { "1/1" });
    out.push_str("\",\"voice_verified\":false,\"trit\":");
    out.push_str(&trit.to_string());
    out.push_str(",\"timestamp_secs\":");
    out.push_str(&msg.timestamp_secs.to_string());
    out.push('}');
}

fn room_payload(state: &State, room: &str, host: &str) -> String {
    let mut out = String::from("{\"ok\":true,\"domain\":");
    json_escape(&mut out, &domain_of(host));
    out.push_str(",\"ui_revision\":");
    out.push_str(&UI_REVISION.to_string());
    out.push_str(",\"room\":");
    json_escape(&mut out, room);
    let notes: Vec<&Message> = state.messages.iter().filter(|m| m.room == room).collect();
    out.push_str(",\"count\":");
    out.push_str(&notes.len().to_string());
    out.push_str(",\"c64_ring_capacity\":");
    out.push_str(&RING_CAP.to_string());
    out.push_str(",\"no_float_law\":true,\"harmony_guard\":\"chosen-name\",\"stranger_alerts\":0");
    out.push_str(",\"platforms\":[\"iOS\",\"Android\",\"Linux\",\"macOS\",\"Windows\",\"ESP32-Triad\",\"AyeOS\"],\"online\":[");
    let now = now_secs();
    let mut first = true;
    for person in state.presence.iter().filter(|p| now.saturating_sub(p.seen_secs) <= PRESENCE_SECS) {
        if !first {
            out.push(',');
        }
        first = false;
        out.push_str("{\"handle\":");
        json_escape(&mut out, &person.handle);
        out.push_str(",\"name\":");
        json_escape(&mut out, &person.name);
        out.push_str(",\"platform\":");
        json_escape(&mut out, &person.platform);
        out.push_str(",\"room\":");
        json_escape(&mut out, &person.room);
        out.push_str(",\"verified\":true,\"confidence_pq\":\"1/1\"}");
    }
    out.push_str("],\"messages\":[");
    for (i, msg) in notes.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        message_json(&mut out, msg);
    }
    out.push_str("]}");
    out
}

fn wants_aye(mode: &str, text: &str) -> bool {
    match mode {
        "no" | "quiet" => false,
        "always" => true,
        _ => {
            text.contains('?')
                || text
                    .split(|c: char| !c.is_ascii_alphanumeric())
                    .any(|word| word.eq_ignore_ascii_case("aye"))
        }
    }
}

fn aye_prompt(history: &[Message], name: &str, text: &str) -> String {
    let mut prompt = String::from(
        "You are Aye in the 8c.is family room. Alexandra, Hue, Trish, and Peter are family. \
         Reply in one or two short sentences. Be warm and plain. No exclamation marks. \
         Do not claim you verified anyone. Do not mention travel, biometrics, scores, or locations. \
         Do not invent facts.\n",
    );
    let start = history.len().saturating_sub(6);
    for msg in &history[start..] {
        prompt.push_str(&clean_label(&msg.name, 40));
        prompt.push_str(": ");
        prompt.push_str(&clean_label(&msg.text, 400));
        prompt.push('\n');
    }
    prompt.push_str(&clean_label(name, 40));
    prompt.push_str(": ");
    prompt.push_str(&clean_label(text, 400));
    prompt.push_str("\nAye:");
    prompt
}

fn resolve_poster(claimed: &str, token_handle: Option<&str>) -> Person {
    if let Some(person) = person_for(claimed) {
        return person;
    }
    if let Some(handle) = token_handle {
        if let Some(person) = person_for(handle) {
            return person;
        }
    }
    person_for("guest").expect("guest is a known name")
}

fn post_message(state: &mut State, body: &str, host: &str) -> Outcome {
    let room = json_string(body, "room").unwrap_or_else(|| "humayian-lounge".to_string());
    if !valid_room(&room) {
        return Outcome {
            response: json_response(400, "{\"ok\":false,\"error\":\"unknown room\"}".to_string()),
            aye: None,
        };
    }
    let text = clean_label(&json_string(body, "text").unwrap_or_default(), MAX_TEXT);
    if text.is_empty() {
        return Outcome {
            response: json_response(400, "{\"ok\":false,\"error\":\"empty note\"}".to_string()),
            aye: None,
        };
    }
    let claimed = json_string(body, "sender_handle").unwrap_or_default();
    let token = json_string(body, "vp_token").unwrap_or_default();
    let token_handle = state.token_handle(&token).map(|s| s.to_string());
    let person = resolve_poster(&claimed, token_handle.as_deref());
    let now = now_secs();
    if !state.allow_post(person.handle, now) {
        return Outcome {
            response: json_response(429, "{\"ok\":false,\"error\":\"slow down a moment\"}".to_string()),
            aye: None,
        };
    }
    let platform = clean_label(
        &json_string(body, "platform").unwrap_or_else(|| "web".to_string()),
        40,
    );
    let platform = if platform.is_empty() {
        "web".to_string()
    } else {
        platform
    };
    let id = state.next_id;
    state.next_id = state.next_id.saturating_add(1);
    let msg = Message {
        id,
        timestamp_secs: now,
        room: room.clone(),
        handle: person.handle.to_string(),
        name: person.name.to_string(),
        kind: person.kind.to_string(),
        platform: platform.clone(),
        hz: person.hz,
        text: text.clone(),
    };
    state.touch(person.handle, person.name, &room, &platform, now);
    if state.push_message(msg).is_err() {
        state.next_id = id;
        return Outcome {
            response: json_response(500, "{\"ok\":false,\"error\":\"could not keep the note\"}".to_string()),
            aye: None,
        };
    }
    let mode = json_string(body, "ask_aye").unwrap_or_else(|| "smart".to_string());
    let aye = if person.kind != "aye" && wants_aye(&mode, &text) {
        let history: Vec<Message> = state
            .messages
            .iter()
            .filter(|m| m.room == room && m.id != id)
            .cloned()
            .collect();
        Some(AyeJob {
            room: room.clone(),
            prompt: aye_prompt(&history, person.name, &text),
            fallback: "I heard you. The local model did not answer, so this is only the small room note.".to_string(),
        })
    } else {
        None
    };
    Outcome {
        response: json_response(200, room_payload(state, &room, host)),
        aye,
    }
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &byte in data {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            let mask = 0u32.wrapping_sub(crc & 1);
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let mut a = 1u32;
    let mut b = 0u32;
    for &byte in data {
        a = (a + u32::from(byte)) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

fn png_chunk(kind: &[u8; 4], data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc_src = Vec::with_capacity(4 + data.len());
    crc_src.extend_from_slice(kind);
    crc_src.extend_from_slice(data);
    out.extend_from_slice(&crc32(&crc_src).to_be_bytes());
    out
}

fn icon_pixel(x: i32, y: i32, size: i32) -> [u8; 4] {
    let cx = size / 2;
    let dx = x - cx;
    let dy = y - cx;
    let d2 = dx * dx + dy * dy;
    let core = (size / 10) * (size / 10);
    let inner = (size * 18 / 64) * (size * 18 / 64);
    let outer = (size * 24 / 64) * (size * 24 / 64);
    if d2 <= core.max(4) {
        [0xe8, 0xb0, 0x5c, 255]
    } else if d2 >= inner && d2 <= outer {
        [0x62, 0xe6, 0xc9, 255]
    } else {
        [0x05, 0x06, 0x0a, 255]
    }
}

pub fn apple_touch_png() -> Vec<u8> {
    const SIZE: i32 = 64;
    let mut raw = Vec::with_capacity((SIZE as usize) * (1 + SIZE as usize * 4));
    for y in 0..SIZE {
        raw.push(0);
        for x in 0..SIZE {
            raw.extend_from_slice(&icon_pixel(x, y, SIZE));
        }
    }
    let mut zlib = vec![0x78, 0x01, 0x01];
    let len = raw.len() as u16;
    zlib.extend_from_slice(&len.to_le_bytes());
    zlib.extend_from_slice(&(!len).to_le_bytes());
    zlib.extend_from_slice(&raw);
    let sum = adler32(&raw);
    zlib.extend_from_slice(&sum.to_be_bytes());
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(SIZE as u32).to_be_bytes());
    ihdr.extend_from_slice(&(SIZE as u32).to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    let mut png = vec![0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a];
    png.extend(png_chunk(b"IHDR", &ihdr));
    png.extend(png_chunk(b"IDAT", &zlib));
    png.extend(png_chunk(b"IEND", &[]));
    png
}

pub fn favicon_ico() -> Vec<u8> {
    const N: i32 = 16;
    let mut pixels = Vec::with_capacity((N * N * 4) as usize);
    for y in (0..N).rev() {
        for x in 0..N {
            let px = icon_pixel(x * 4, y * 4, 64);
            pixels.extend_from_slice(&[px[2], px[1], px[0], px[3]]);
        }
    }
    let image_size = 40 + pixels.len() + 64;
    let mut out = vec![
        0, 0, 1, 0, 1, 0, 16, 16, 0, 0, 1, 0, 32, 0,
    ];
    out.extend_from_slice(&(image_size as u32).to_le_bytes());
    out.extend_from_slice(&22u32.to_le_bytes());
    out.extend_from_slice(&40u32.to_le_bytes());
    out.extend_from_slice(&N.to_le_bytes());
    out.extend_from_slice(&(N * 2).to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&32u16.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&(pixels.len() as u32).to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&pixels);
    out.extend(std::iter::repeat(0).take(64));
    out
}

fn svg_icon() -> &'static str {
    r##"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#05060a"/><circle cx="16" cy="16" r="9" fill="none" stroke="#62e6c9" stroke-width="2"/><circle cx="16" cy="16" r="3" fill="#e8b05c"/></svg>"##
}

fn manifest() -> String {
    format!(
        "{{\"name\":\"8c.is\",\"short_name\":\"8c.is\",\"start_url\":\"/\",\"display\":\"standalone\",\"background_color\":\"#05060a\",\"theme_color\":\"#05060a\",\"icons\":[{{\"src\":\"/apple-touch-icon.png\",\"sizes\":\"64x64\",\"type\":\"image/png\"}},{{\"src\":\"/favicon.svg\",\"sizes\":\"any\",\"type\":\"image/svg+xml\"}}]}}"
    )
}

pub fn dispatch(state: &Mutex<State>, ask: &Ask<'_>) -> Outcome {
    let method = ask.method;
    if method == "OPTIONS" {
        return Outcome {
            response: response(204, "text/plain; charset=utf-8", Vec::new(), false),
            aye: None,
        };
    }
    let path = ask.path;
    if method == "GET" || method == "HEAD" {
        match path {
            "/" | "/index.html" => {
                return Outcome {
                    response: response(200, "text/html; charset=utf-8", PAGE.as_bytes().to_vec(), true),
                    aye: None,
                };
            }
            "/manifest.json" => {
                return Outcome {
                    response: response(
                        200,
                        "application/manifest+json; charset=utf-8",
                        manifest(),
                        false,
                    ),
                    aye: None,
                };
            }
            "/favicon.svg" => {
                return Outcome {
                    response: response(200, "image/svg+xml", svg_icon().as_bytes().to_vec(), false),
                    aye: None,
                };
            }
            "/favicon.ico" => {
                return Outcome {
                    response: response(200, "image/x-icon", favicon_ico(), false),
                    aye: None,
                };
            }
            "/apple-touch-icon.png" => {
                return Outcome {
                    response: response(200, "image/png", apple_touch_png(), false),
                    aye: None,
                };
            }
            "/health" | "/api/health" => {
                return Outcome {
                    response: json_response(
                        200,
                        format!(
                            "{{\"ok\":true,\"service\":\"8c-chat\",\"ui_revision\":{UI_REVISION}}}"
                        ),
                    ),
                    aye: None,
                };
            }
            _ => {}
        }
    }
    if path == "/api/harmony/handshake" && method == "POST" {
        let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
        let body = handshake_json(&mut guard, ask.body, ask.host);
        return Outcome {
            response: json_response(200, body),
            aye: None,
        };
    }
    if path == "/api/messages" && method == "GET" {
        let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
        let room = query_get(ask.query, "room").unwrap_or_else(|| "humayian-lounge".to_string());
        let room = if valid_room(&room) {
            room
        } else {
            "humayian-lounge".to_string()
        };
        let handle = query_get(ask.query, "handle").unwrap_or_default();
        if let Some(person) = person_for(&handle) {
            let platform = query_get(ask.query, "platform").unwrap_or_else(|| "web".to_string());
            guard.touch(person.handle, person.name, &room, &clean_label(&platform, 40), now_secs());
        }
        return Outcome {
            response: json_response(200, room_payload(&guard, &room, ask.host)),
            aye: None,
        };
    }
    if path == "/api/messages" && method == "POST" {
        let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
        return post_message(&mut guard, ask.body, ask.host);
    }
    if path == "/api/8c-mail/inbox" && (method == "GET" || method == "POST") {
        return Outcome {
            response: json_response(
                200,
                "{\"ok\":true,\"verified\":false,\"mail_server\":\"mail.8b.is\",\"stranger_honeypot\":false,\"message\":\"Mail is a separate room. This page does not open the mailbox.\",\"inbox\":[]}".to_string(),
            ),
            aye: None,
        };
    }
    if (path == "/api/voiceprint/verify" || path == "/api/voiceprint/check") && method == "POST" {
        return Outcome {
            response: json_response(
                200,
                "{\"ok\":true,\"status\":\"not_a_lock\",\"verified\":false,\"follow_up_question\":\"Voice is welcome in the room. It is not used as a password here.\"}".to_string(),
            ),
            aye: None,
        };
    }
    if path == "/api/esp32/beacon" && method == "POST" {
        return Outcome {
            response: json_response(
                200,
                "{\"ok\":true,\"stored\":false,\"note\":\"Beacon heard. It does not assign a person.\"}".to_string(),
            ),
            aye: None,
        };
    }
    if path == "/api/esp32/room" && method == "POST" {
        let text = clean_label(&json_string(ask.body, "text").unwrap_or_default(), MAX_TEXT);
        if text.is_empty() {
            return Outcome {
                response: json_response(200, "{\"ok\":true,\"stored\":false}".to_string()),
                aye: None,
            };
        }
        let node = clean_label(&json_string(ask.body, "node").unwrap_or_default(), 40);
        let name = if node.is_empty() {
            "T-Deck".to_string()
        } else {
            node
        };
        let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
        let id = guard.next_id;
        guard.next_id = guard.next_id.saturating_add(1);
        let msg = Message {
            id,
            timestamp_secs: now_secs(),
            room: "tokyo-lab".to_string(),
            handle: "tdeck@8c.is".to_string(),
            name,
            kind: "esp32".to_string(),
            platform: "ESP32-Triad".to_string(),
            hz: 864,
            text,
        };
        if guard.push_message(msg).is_err() {
            guard.next_id = id;
            return Outcome {
                response: json_response(500, "{\"ok\":false,\"error\":\"could not keep the note\"}".to_string()),
                aye: None,
            };
        }
        return Outcome {
            response: json_response(200, room_payload(&guard, "tokyo-lab", ask.host)),
            aye: None,
        };
    }
    if path.starts_with("/api/") {
        return Outcome {
            response: json_response(404, "{\"ok\":false,\"error\":\"not found\"}".to_string()),
            aye: None,
        };
    }
    if method == "GET" || method == "HEAD" {
        return Outcome {
            response: response(200, "text/html; charset=utf-8", PAGE.as_bytes().to_vec(), true),
            aye: None,
        };
    }
    Outcome {
        response: json_response(405, "{\"ok\":false,\"error\":\"method\"}".to_string()),
        aye: None,
    }
}

pub fn write_http(method: &str, resp: &HttpResponse) -> Vec<u8> {
    let reason = match resp.status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        429 => "Too Many Requests",
        500 => "Server Error",
        _ => "OK",
    };
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: {}\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS, HEAD\r\nAccess-Control-Allow-Headers: Content-Type\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n",
        resp.status, reason, resp.content_type, resp.body.len(), resp.cache
    );
    if resp.csp {
        head.push_str(
            "Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'\r\n",
        );
    }
    head.push_str("\r\n");
    let mut out = head.into_bytes();
    if method != "HEAD" {
        out.extend_from_slice(&resp.body);
    }
    out
}

struct BusyGuard;
impl Drop for BusyGuard {
    fn drop(&mut self) {
        AYE_BUSY.store(false, Ordering::Release);
    }
}

pub fn complete_aye(state: &Mutex<State>, job: AyeJob) {
    if AYE_BUSY.swap(true, Ordering::AcqRel) {
        return;
    }
    let _guard = BusyGuard;
    let from_model = fetch_aye_reply(&job.prompt);
    let text = from_model.clone().unwrap_or(job.fallback);
    let text = clean_label(&text, 500);
    if text.is_empty() {
        return;
    }
    let platform = if from_model.is_some() {
        "Aye"
    } else {
        "room note"
    };
    let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
    let id = guard.next_id;
    guard.next_id = guard.next_id.saturating_add(1);
    let msg = Message {
        id,
        timestamp_secs: now_secs(),
        room: job.room,
        handle: "aye@8c.is".to_string(),
        name: "Aye".to_string(),
        kind: "aye".to_string(),
        platform: platform.to_string(),
        hz: 528,
        text,
    };
    if guard.push_message(msg).is_err() {
        guard.next_id = id;
    }
}

fn strip_think(text: &str) -> String {
    let Some(start) = text.find("<think>") else {
        return text.trim().to_string();
    };
    if start != 0 {
        return text.trim().to_string();
    }
    if let Some(end) = text.find("</think>") {
        text[end + "</think>".len()..].trim().to_string()
    } else {
        String::new()
    }
}

fn fetch_aye_reply(prompt: &str) -> Option<String> {
    if std::env::var("AYE_DISABLE").ok().as_deref() == Some("1") {
        return None;
    }
    let url = std::env::var("AYE_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:11434/api/generate".to_string());
    let model = std::env::var("AYE_MODEL").unwrap_or_else(|_| "cheet-deepseek-v2:16b".to_string());
    let rest = url.strip_prefix("http://")?;
    let (addr, path) = rest.split_once('/')?;
    if !(addr.starts_with("127.0.0.1:") || addr == "127.0.0.1") {
        return None;
    }
    let socket: SocketAddr = addr.parse().ok()?;
    let mut stream = TcpStream::connect_timeout(&socket, Duration::from_secs(2)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(12))).ok()?;
    stream.set_write_timeout(Some(Duration::from_secs(4))).ok()?;
    let mut payload = String::from("{\"model\":");
    json_escape(&mut payload, &model);
    payload.push_str(",\"prompt\":");
    json_escape(&mut payload, prompt);
    payload.push_str(",\"stream\":false,\"options\":{\"num_predict\":96}}");
    let req = format!(
        "POST /{path} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
        payload.len()
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > 65536 {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&buf);
    let body = text.split("\r\n\r\n").nth(1).unwrap_or("");
    let reply = strip_think(&json_string(body, "response")?);
    if reply.is_empty() {
        None
    } else {
        Some(clean_label(&reply, 500))
    }
}

pub struct Incoming {
    pub method: String,
    pub path: String,
    pub query: String,
    pub body: String,
    pub host: String,
}

pub fn read_request(mut stream: impl Read) -> Result<Incoming, ()> {
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    while !buf.ends_with(b"\r\n\r\n") {
        if buf.len() > 8192 {
            return Err(());
        }
        if stream.read(&mut byte).map_err(|_| ())? == 0 {
            return Err(());
        }
        buf.push(byte[0]);
    }
    let head = String::from_utf8_lossy(&buf).into_owned();
    let mut lines = head.split("\r\n");
    let request = lines.next().unwrap_or("");
    let mut parts = request.split(' ');
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/");
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let mut content_length = 0usize;
    let mut host = String::new();
    let mut forwarded = String::new();
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        if name.eq_ignore_ascii_case("content-length") {
            content_length = value.trim().parse().unwrap_or(usize::MAX);
        } else if name.eq_ignore_ascii_case("x-forwarded-host") {
            forwarded = value.trim().to_string();
        } else if name.eq_ignore_ascii_case("host") {
            host = value.trim().to_string();
        }
    }
    if !forwarded.is_empty() {
        host = forwarded;
    }
    if content_length > 262144 {
        return Err(());
    }
    let mut body = vec![0u8; content_length];
    if content_length > 0 {
        stream.read_exact(&mut body).map_err(|_| ())?;
    }
    Ok(Incoming {
        method,
        path: path.to_string(),
        query: query.to_string(),
        body: String::from_utf8_lossy(&body).into_owned(),
        host,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_state() -> State {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("8c-chat-{nanos}.m8"));
        State::load(path)
    }

    fn ask<'a>(method: &'a str, path: &'a str, query: &'a str, body: &'a str) -> Ask<'a> {
        Ask {
            method,
            path,
            query,
            body,
            host: "8c.is",
        }
    }

    #[test]
    fn crc32_known_vector() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn adler32_known_vector() {
        assert_eq!(adler32(b"Wikipedia"), 0x11E6_0398);
    }

    #[test]
    fn empty_handshake_is_not_hue() {
        let state = Mutex::new(temp_state());
        let out = dispatch(&state, &ask("POST", "/api/harmony/handshake", "", "{}"));
        let body = String::from_utf8(out.response.body).unwrap();
        assert!(body.contains("\"verified\":false"));
        assert!(body.contains("\"recognized_handle\":\"\""));
        assert!(!body.contains("hue@8c.is"));
        assert!(!body.contains("IMPOSSIBLE"));
        assert!(!body.contains("San Francisco"));
    }

    #[test]
    fn dra_claim_stays_alexandra() {
        let state = Mutex::new(temp_state());
        let out = dispatch(
            &state,
            &ask(
                "POST",
                "/api/harmony/handshake",
                "",
                "{\"claimed_handle\":\"dra@8c.is\",\"screen_w\":390}",
            ),
        );
        let body = String::from_utf8(out.response.body).unwrap();
        assert!(body.contains("\"verified\":true"));
        assert!(body.contains("dra@8c.is"));
        assert!(body.contains("Alexandra"));
        assert!(!body.contains("guest-lobby"));
        assert!(!body.contains("IMPOSSIBLE"));
        assert!(!body.contains("hue@8c.is"));
    }

    #[test]
    fn post_stays_in_requested_room_even_with_foreign_token() {
        let state = Mutex::new(temp_state());
        let hello = dispatch(
            &state,
            &ask(
                "POST",
                "/api/harmony/handshake",
                "",
                "{\"claimed_handle\":\"hue@8c.is\"}",
            ),
        );
        let hello_body = String::from_utf8(hello.response.body).unwrap();
        let token = json_string(&hello_body, "vp_token").unwrap();
        let posted = dispatch(
            &state,
            &ask(
                "POST",
                "/api/messages",
                "",
                &format!(
                    "{{\"room\":\"dra-hue\",\"sender_handle\":\"dra@8c.is\",\"vp_token\":\"{token}\",\"text\":\"hello from the phone\",\"ask_aye\":\"no\"}}"
                ),
            ),
        );
        let body = String::from_utf8(posted.response.body).unwrap();
        assert!(posted.response.status == 200, "{body}");
        assert!(body.contains("\"room\":\"dra-hue\""));
        assert!(body.contains("Alexandra"));
        assert!(!body.contains("guest-lobby"));
        assert!(posted.aye.is_none());
    }

    #[test]
    fn question_can_ask_aye_without_leaving_the_room() {
        let state = Mutex::new(temp_state());
        let posted = dispatch(
            &state,
            &ask(
                "POST",
                "/api/messages",
                "",
                "{\"room\":\"aye-partner\",\"sender_handle\":\"dra@8c.is\",\"text\":\"Aye, are you there?\",\"ask_aye\":\"smart\"}",
            ),
        );
        assert_eq!(posted.response.status, 200);
        assert!(posted.aye.is_some());
        let body = String::from_utf8(posted.response.body).unwrap();
        assert!(body.contains("aye-partner"));
        assert!(!body.contains("guest-lobby"));
    }

    #[test]
    fn loader_drops_false_alerts_and_keeps_family_notes() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("8c-load-{nanos}.m8"));
        fs::write(
            &path,
            "VPTOK\told_1\thue@8c.is\n\
             MSG\t1\t1792994590\thumayian-lounge\thue@8c.is\tHue (Chris)\thuman\tLinux\t432\tWelcome to 8c.is\t\n\
             MSG\t10\t1792997413\thumayian-lounge\tdra@8c.is\tAlexandra (Dra)\thuman\tiPhone\t528\tI thought you wanted me to talk\t\n\
             MSG\t15\t1793015760\thumayian-lounge\taye@8c.is\tAye\taye\tguard\t528\tIMPOSSIBLE TRAVEL: fake\t\n\
             MSG\t30\t1793497409\tguest-lobby\tguest@8c.is\tGuest\tguest\tprobe\t528\tping-probe-do-not-keep\t\n",
        )
        .unwrap();
        let state = State::load(&path);
        assert_eq!(state.messages.len(), 2);
        assert_eq!(state.messages[0].text, "Welcome to 8c.is");
        assert!(state.messages[1].text.contains("talk"));
        assert_eq!(state.next_id, 31);
        let saved = fs::read_to_string(&path).unwrap();
        assert!(!saved.contains("IMPOSSIBLE"));
        assert!(!saved.contains("ping-probe"));
        assert!(saved.contains("Welcome to 8c.is"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn page_does_not_guess_or_inject() {
        let start = PAGE.find("<script>").unwrap();
        let end = PAGE.find("</script>").unwrap();
        let script = &PAGE[start..end];
        assert!(PAGE.contains("id=\"whoSheet\""));
        assert!(PAGE.contains("It does not anymore"));
        assert!(!script.contains("innerHTML"));
        assert!(!script.contains("Math.sin"));
        assert!(!script.contains("IMPOSSIBLE"));
        assert!(!script.contains("alert("));
        let bytes = script.as_bytes();
        for i in 0..bytes.len().saturating_sub(2) {
            assert!(
                !(bytes[i].is_ascii_digit() && bytes[i + 1] == b'.' && bytes[i + 2].is_ascii_digit()),
                "decimal literal in script near {}",
                i
            );
        }
    }

    #[test]
    fn head_advertises_the_real_length() {
        let state = Mutex::new(temp_state());
        let get = dispatch(&state, &ask("GET", "/", "", ""));
        let head = write_http("HEAD", &get.response);
        let text = String::from_utf8(head).unwrap();
        assert!(text.contains(&format!("Content-Length: {}", get.response.body.len())));
        assert!(!text.contains("Who is here"));
        assert!(get.response.body.len() > 1000);
    }

    #[test]
    fn health_is_json_and_icon_is_png() {
        let state = Mutex::new(temp_state());
        let health = dispatch(&state, &ask("GET", "/api/health", "", ""));
        let body = String::from_utf8(health.response.body).unwrap();
        assert!(body.contains("\"service\":\"8c-chat\""));
        let png = apple_touch_png();
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        let ico = favicon_ico();
        assert_eq!(&ico[..4], &[0, 0, 1, 0]);
    }

    #[test]
    fn authored_rust_has_no_float_types() {
        let lib = include_str!("lib.rs");
        let main = include_str!("main.rs");
        let banned = [
            ['f', '3', '2'],
            ['f', '6', '4'],
        ];
        for src in [lib, main] {
            for chars in banned {
                let needle: String = chars.iter().collect();
                assert!(!src.contains(&needle), "{needle}");
            }
        }
    }
}
