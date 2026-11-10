//! Append-only chat journal, not a MEM8 file. Keep the original legacy log intact.
use crate::json::{self, Json};
use std::cell::Cell;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub struct Store {
    file: File,
    poisoned: Cell<bool>,
    source: String,
}
impl Store {
    pub fn open(
        path: &Path,
        mut replay: impl FnMut(Json) -> Result<(), String>,
    ) -> Result<Self, String> {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut options = OpenOptions::new();
        options.create(true).append(true).read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(path).map_err(|e| e.to_string())?;
        if !file.metadata().map_err(|e| e.to_string())?.is_file() {
            return Err("Journal must be a regular file".into());
        }
        file.try_lock()
            .map_err(|e| format!("Journal already owned or unavailable: {e}"))?;
        file.sync_all().map_err(|e| e.to_string())?;
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            File::open(parent)
                .and_then(|f| f.sync_all())
                .map_err(|e| e.to_string())?;
        }
        File::open(".").and_then(|f| f.sync_all()).map_err(|e| e.to_string())?;
        {
            let mut reader = BufReader::new(&file);
            let mut count = 0u64;
            loop {
                let mut line = Vec::new();
                let n = reader
                    .by_ref()
                    .take(131_073)
                    .read_until(b'\n', &mut line)
                    .map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                count += 1;
                if line.len() > 131_072 || !line.ends_with(b"\n") {
                    return Err(format!(
                        "incomplete/oversize journal record {count}; original retained"
                    ));
                }
                let line = std::str::from_utf8(&line).map_err(|e| e.to_string())?;
                replay(json::parse(line).map_err(|e| format!("journal record {count}: {e}"))?)?;
            }
        }
        // Separate from the delivery journal so revision-8 recovery can still replay it.
        let source_path = path.with_extension("memory-source");
        if !source_path.exists() {
            let temporary = source_path.with_extension(format!("{}.new", random_id()?));
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut source_file = options.open(&temporary).map_err(|e| e.to_string())?;
            source_file
                .write_all(random_id()?.as_bytes())
                .and_then(|_| source_file.sync_all())
                .map_err(|e| e.to_string())?;
            fs::rename(temporary, &source_path).map_err(|e| e.to_string())?;
            File::open(
                source_path
                    .parent()
                    .filter(|p| !p.as_os_str().is_empty())
                    .unwrap_or(Path::new(".")),
            )
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
        }
        let mut source = String::new();
        File::open(&source_path)
            .and_then(|f| f.take(49).read_to_string(&mut source))
            .map_err(|e| e.to_string())?;
        if source.len() != 48 || !source.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("Invalid memory source identity; original retained".into());
        }
        // Ensure the source string is treated as a fixed-size identity
        let mut source_bytes = [0u8; 48];
        source_bytes.copy_from_slice(source.as_bytes());
        Ok(Self {
            file,
            poisoned: Cell::new(false),
            source,
        })
    }
    /// Byte-cursor replay, bounded to 16 journal records per call. Projection happens
    /// in Chat so credentials, job prompts, leases and client IDs never leave here.
    pub fn memory_page(&self, cursor: u64, project: impl Fn(Json) -> Json) -> Result<Json, String> {
        let mut file = &self.file;
        let end = file.metadata().map_err(|e| e.to_string())?.len();
        if cursor > end {
            return Err("Memory cursor exceeds journal; recovery required".into());
        }
        if cursor > 0 {
            file.seek(SeekFrom::Start(cursor - 1))
                .map_err(|e| e.to_string())?;
            let mut previous = [0];
            file.read_exact(&mut previous).map_err(|e| e.to_string())?;
            if previous != *b"\n" {
                return Err("Memory cursor is not a record boundary".into());
            }
        }
        file.seek(SeekFrom::Start(cursor))
            .map_err(|e| e.to_string())?;
        let mut reader = BufReader::new(file);
        let mut next = cursor;
        let mut events = Vec::new();
        for _ in 0..16 {
            let mut line = Vec::new();
            let size = reader
                .by_ref()
                .take(131_073)
                .read_until(b'\n', &mut line)
                .map_err(|e| e.to_string())?;
            if size == 0 {
                break;
            }
            if size > 131_072 || !line.ends_with(b"\n") {
                return Err("Journal needs recovery".into());
            }
            next = next
                .checked_add(size as u64)
                .ok_or("Memory cursor overflow")?;
            let event = project(json::parse(
                std::str::from_utf8(&line).map_err(|e| e.to_string())?,
            )?);
            if event != Json::Null {
                events.push(crate::obj! {"cursor" => next, "event" => event});
            }
        }
        Ok(
            crate::obj! {"ok" => true, "source" => self.source.clone(), "after" => cursor, "next" => next, "caught_up" => next == end, "events" => events},
        )
    }
    pub fn append(&self, value: &Json) -> Result<(), String> {
        let mut bytes = value.encode().into_bytes();
        bytes.push(b'\n');
        if bytes.len() > 131_072 {
            return Err("journal record too large".into());
        }
        if self.poisoned.get() {
            return Err("could not save: journal needs recovery".into());
        }
        let mut file = &self.file;
        let before = file.metadata().map_err(|e| e.to_string())?.len();
        if let Err(e) = file.write_all(&bytes).and_then(|_| file.sync_data()) {
            if file.set_len(before).and_then(|_| file.sync_data()).is_err() {
                self.poisoned.set(true);
            }
            return Err(format!("could not save: {e}"));
        }
        Ok(())
    }
    /// Slow-path duplicate lookup: recent sends use RAM; older retries use the durable journal.
    /// Reads are bounded to one record at a time. This also permits recovery after ring eviction.
    pub fn find_request(&self, handle: &str, client: &str) -> Result<Option<Json>, String> {
        let mut file = &self.file;
        file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
        let mut reader = BufReader::new(file);
        loop {
            let mut line = Vec::new();
            let size = reader
                .by_ref()
                .take(131_073)
                .read_until(b'\n', &mut line)
                .map_err(|e| e.to_string())?;
            if size == 0 {
                return Ok(None);
            }
            if size > 131_072 || !line.ends_with(b"\n") {
                return Err("Journal needs recovery".into());
            }
            let record = json::parse(std::str::from_utf8(&line).map_err(|e| e.to_string())?)?;
            let msg = record.get("message");
            if msg.get("sender_handle").text() == handle && msg.get("client_id").text() == client {
                return Ok(Some(msg.clone()));
            }
        }
    }
}

pub fn random_id() -> Result<String, String> {
    let mut bytes = [0u8; 24];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_append_does_not_claim_success_or_rewrite_existing_records() {
        let path = std::env::temp_dir().join(format!("8c-store-{}", random_id().unwrap()));
        let store = Store::open(&path, |_| Ok(())).unwrap();
        store.append(&crate::obj! {"type" => "test"}).unwrap();
        drop(store);
        let original = fs::read(&path).unwrap();
        let store = Store {
            file: File::open(&path).unwrap(),
            poisoned: Cell::new(false),
            source: String::new(),
        };
        assert!(store.append(&crate::obj! {"type" => "another"}).is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        assert!(store.append(&crate::obj! {"type" => "again"}).is_err());
        drop(store);
        fs::remove_file(path.with_extension("memory-source")).unwrap();
        fs::remove_file(path).unwrap();
    }
}
