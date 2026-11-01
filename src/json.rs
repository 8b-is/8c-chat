//! Bounded JSON codec. Numbers retain their wire spelling; no floating arithmetic.
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Json {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Json>),
    Object(BTreeMap<String, Json>),
}
impl Json {
    pub fn get(&self, key: &str) -> &Self {
        match self {
            Self::Object(map) => map.get(key).unwrap_or(&Self::Null),
            _ => &Self::Null,
        }
    }
    pub fn text(&self) -> &str {
        if let Self::String(s) = self {
            s
        } else {
            ""
        }
    }
    pub fn number(&self) -> u64 {
        if let Self::Number(s) = self {
            s.parse().unwrap_or(0)
        } else {
            0
        }
    }
    pub fn boolean(&self) -> bool {
        matches!(self, Self::Bool(true))
    }
    pub fn rows(&self) -> &[Self] {
        if let Self::Array(v) = self {
            v
        } else {
            &[]
        }
    }
    pub fn set(&mut self, key: &str, value: impl Into<Self>) {
        if let Self::Object(map) = self {
            map.insert(key.into(), value.into());
        }
    }
    pub fn encode(&self) -> String {
        match self {
            Self::Null => "null".into(),
            Self::Bool(b) => b.to_string(),
            Self::Number(n) => n.clone(),
            Self::String(s) => quote(s),
            Self::Array(v) => format!(
                "[{}]",
                v.iter().map(Self::encode).collect::<Vec<_>>().join(",")
            ),
            Self::Object(m) => format!(
                "{{{}}}",
                m.iter()
                    .map(|(k, v)| format!("{}:{}", quote(k), v.encode()))
                    .collect::<Vec<_>>()
                    .join(",")
            ),
        }
    }
}
impl From<&str> for Json {
    fn from(v: &str) -> Self {
        Self::String(v.into())
    }
}
impl From<String> for Json {
    fn from(v: String) -> Self {
        Self::String(v)
    }
}
impl From<u64> for Json {
    fn from(v: u64) -> Self {
        Self::Number(v.to_string())
    }
}
impl From<bool> for Json {
    fn from(v: bool) -> Self {
        Self::Bool(v)
    }
}
impl From<Vec<Json>> for Json {
    fn from(v: Vec<Json>) -> Self {
        Self::Array(v)
    }
}

#[macro_export]
macro_rules! obj {
    ($($key:literal => $value:expr),* $(,)?) => {{
        let mut map = std::collections::BTreeMap::new();
        $(map.insert($key.to_string(), $crate::json::Json::from($value));)*
        $crate::json::Json::Object(map)
    }};
}

pub fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c < ' ' => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

pub fn parse(s: &str) -> Result<Json, String> {
    if s.len() > 1_048_576 {
        return Err("JSON too large".into());
    }
    let mut parser = Parser {
        s: s.as_bytes(),
        at: 0,
    };
    let value = parser.value(0)?;
    parser.space();
    if parser.at != parser.s.len() {
        return Err("trailing JSON".into());
    }
    Ok(value)
}
struct Parser<'a> {
    s: &'a [u8],
    at: usize,
}
impl Parser<'_> {
    fn space(&mut self) {
        while self
            .s
            .get(self.at)
            .is_some_and(|c| matches!(c, b' ' | b'\n' | b'\r' | b'\t'))
        {
            self.at += 1;
        }
    }
    fn take(&mut self, c: u8) -> bool {
        if self.s.get(self.at) == Some(&c) {
            self.at += 1;
            true
        } else {
            false
        }
    }
    fn require(&mut self, c: u8) -> Result<(), String> {
        if self.take(c) {
            Ok(())
        } else {
            Err("invalid JSON delimiter".into())
        }
    }
    fn hex(&mut self) -> Result<u16, String> {
        let mut n = 0u16;
        for _ in 0..4 {
            let c = *self.s.get(self.at).ok_or("short Unicode escape")?;
            self.at += 1;
            n = (n << 4)
                | match c {
                    b'0'..=b'9' => u16::from(c - b'0'),
                    b'a'..=b'f' => u16::from(c - b'a' + 10),
                    b'A'..=b'F' => u16::from(c - b'A' + 10),
                    _ => return Err("invalid Unicode escape".into()),
                };
        }
        Ok(n)
    }
    fn string(&mut self) -> Result<String, String> {
        self.require(b'"')?;
        let mut bytes = Vec::new();
        loop {
            let c = *self.s.get(self.at).ok_or("unterminated string")?;
            self.at += 1;
            match c {
                b'"' => return String::from_utf8(bytes).map_err(|_| "invalid UTF-8".into()),
                b'\\' => {
                    let e = *self.s.get(self.at).ok_or("short escape")?;
                    self.at += 1;
                    match e {
                        b'"' | b'\\' | b'/' => bytes.push(e),
                        b'n' => bytes.push(b'\n'),
                        b'r' => bytes.push(b'\r'),
                        b't' => bytes.push(b'\t'),
                        b'b' => bytes.push(8),
                        b'f' => bytes.push(12),
                        b'u' => {
                            let h = self.hex()?;
                            let code = if (0xD800..=0xDBFF).contains(&h) {
                                self.require(b'\\')?;
                                self.require(b'u')?;
                                let l = self.hex()?;
                                if !(0xDC00..=0xDFFF).contains(&l) {
                                    return Err("invalid surrogate pair".into());
                                }
                                0x10000 + ((u32::from(h) - 0xD800) << 10) + u32::from(l) - 0xDC00
                            } else {
                                u32::from(h)
                            };
                            let ch = char::from_u32(code).ok_or("invalid Unicode scalar")?;
                            bytes.extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes());
                        }
                        _ => return Err("invalid escape".into()),
                    }
                }
                0..=31 => return Err("unescaped control character".into()),
                c => bytes.push(c),
            }
        }
    }
    fn value(&mut self, depth: u32) -> Result<Json, String> {
        if depth > 24 {
            return Err("JSON nesting limit".into());
        }
        self.space();
        let first = *self.s.get(self.at).ok_or("missing JSON value")?;
        match first {
            b'"' => Ok(Json::String(self.string()?)),
            b'{' => {
                self.at += 1;
                self.space();
                let mut map = BTreeMap::new();
                if self.take(b'}') {
                    return Ok(Json::Object(map));
                }
                loop {
                    self.space();
                    let key = self.string()?;
                    self.space();
                    self.require(b':')?;
                    let value = self.value(depth + 1)?;
                    if map.insert(key, value).is_some() {
                        return Err("duplicate JSON key".into());
                    }
                    self.space();
                    if self.take(b'}') {
                        break;
                    }
                    self.require(b',')?;
                }
                Ok(Json::Object(map))
            }
            b'[' => {
                self.at += 1;
                self.space();
                let mut values = Vec::new();
                if self.take(b']') {
                    return Ok(Json::Array(values));
                }
                loop {
                    values.push(self.value(depth + 1)?);
                    self.space();
                    if self.take(b']') {
                        break;
                    }
                    self.require(b',')?;
                }
                Ok(Json::Array(values))
            }
            b't' | b'f' | b'n' => {
                let (word, value): (&[u8], Json) = match first {
                    b't' => (b"true", Json::Bool(true)),
                    b'f' => (b"false", Json::Bool(false)),
                    _ => (b"null", Json::Null),
                };
                if !self.s[self.at..].starts_with(word) {
                    return Err("invalid literal".into());
                }
                self.at += word.len();
                Ok(value)
            }
            b'-' | b'0'..=b'9' => {
                let start = self.at;
                self.take(b'-');
                if !self.take(b'0') {
                    self.digits()?;
                }
                if self.take(b'.') {
                    self.digits()?;
                }
                if self.take(b'e') || self.take(b'E') {
                    if !self.take(b'+') {
                        self.take(b'-');
                    }
                    self.digits()?;
                }
                Ok(Json::Number(
                    String::from_utf8(self.s[start..self.at].to_vec())
                        .map_err(|_| "invalid number")?,
                ))
            }
            _ => Err("invalid JSON value".into()),
        }
    }
    fn digits(&mut self) -> Result<(), String> {
        let start = self.at;
        while self.s.get(self.at).is_some_and(u8::is_ascii_digit) {
            self.at += 1;
        }
        if start == self.at {
            Err("missing digits".into())
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_and_structural_keys() {
        let value =
            parse(r#"{"text":"\"name\":\"wrong\" \ud83d\udc99","name":"Éva 東京"}"#).unwrap();
        assert_eq!(value.get("name").text(), "Éva 東京");
        assert!(value.get("text").text().ends_with('💙'));
        assert_eq!(parse(&value.encode()).unwrap(), value);
        for bad in [
            r#"{"x":1,"x":2}"#,
            r#""\ud800""#,
            r#""\uéあ""#,
            "[01]",
            "1.",
            "true false",
            "[1,]",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
    }
}
