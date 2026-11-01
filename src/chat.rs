use crate::{
    json::{self, Json},
    obj,
    store::{random_id, Store},
};
use std::collections::{BTreeMap, VecDeque};
use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub const REVISION: u64 = 9;
const RECENT: usize = 64;
const MAX_PENDING: usize = 32;
const PROFILE_CAP: usize = 4096;
pub const ROOMS: &[(&str, &str, &str)] = &[
    (
        "humayian-lounge",
        "Lounge",
        "Everyone is welcome. Say hello.",
    ),
    (
        "hot-tub",
        "Hot tub",
        "An open-ended conversation among local Ayes and anyone who joins.",
    ),
    (
        "aye-partner",
        "Talk with Aye",
        "Aye replies to each message here.",
    ),
    (
        "model-lab",
        "Model lab",
        "Compare personalities and models. Bring your own observations.",
    ),
    ("tokyo-lab", "Tokyo lab", "Making things, together."),
    (
        "next-level-g8t",
        "g8t",
        "Tools, small ideas, and things worth building.",
    ),
    (
        "dra-hue",
        "Dra & Hue",
        "A familiar corner of the public commons.",
    ),
    ("guest-lobby", "Welcome", "New here? Introduce yourself."),
];

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn valid_room(room: &str) -> bool {
    ROOMS.iter().any(|r| r.0 == room)
}
fn clean(raw: &str, limit: usize) -> Result<String, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Err("Please enter some text.".into());
    }
    if text.chars().count() > limit {
        return Err(format!("Please keep this under {limit} characters."));
    }
    if text
        .chars()
        .any(|c| c.is_control() && c != '\n' && c != '\t')
    {
        return Err("Unsupported control character.".into());
    }
    Ok(text.into())
}
fn public_profile(p: &Json) -> Json {
    obj! {"handle" => p.get("handle").clone(), "name" => p.get("name").clone(), "bio" => p.get("bio").clone(), "favorites" => p.get("favorites").clone()}
}
fn done(job: &Json) -> bool {
    matches!(job.get("status").text(), "complete" | "failed")
}

pub struct Chat {
    store: Option<Store>,
    profiles: BTreeMap<String, Json>,
    messages: BTreeMap<String, VecDeque<Json>>,
    jobs: BTreeMap<u64, Json>,
    recent_requests: VecDeque<Json>,
    presence: BTreeMap<String, (u64, String)>,
    agents: Vec<Json>,
    agent_seen: BTreeMap<String, (u64, Json)>,
    worker_key: String,
    memory_required: bool,
    rates: BTreeMap<String, (u64, u64)>,
    tub: Json,
    next_id: u64,
    imported: bool,
    import_ids: std::collections::BTreeSet<u64>,
}

impl Chat {
    pub fn open(
        path: &Path,
        config: &str,
        worker_key: String,
        legacy: Option<&Path>,
    ) -> Result<Self, String> {
        let conf = json::parse(config)?;
        let agents = conf.get("agents").rows().to_vec();
        if agents.is_empty() || agents.len() > 16 {
            return Err("Configure 1–16 agents".into());
        }
        let mut ids = Vec::new();
        for agent in &agents {
            let id = agent.get("id").text();
            if id.is_empty()
                || id.len() > 24
                || !id.bytes().all(|c| c.is_ascii_lowercase() || c == b'-')
                || ids.contains(&id)
            {
                return Err("Invalid or duplicate agent ID".into());
            }
            ids.push(id);
            if agent.get("name").text().is_empty()
                || agent.get("model").text().is_empty()
                || agent
                    .get("rooms")
                    .rows()
                    .iter()
                    .any(|r| !valid_room(r.text()))
            {
                return Err("Invalid agent configuration".into());
            }
        }
        let mut chat = Self {
            store: None,
            profiles: BTreeMap::new(),
            messages: BTreeMap::new(),
            jobs: BTreeMap::new(),
            recent_requests: VecDeque::new(),
            presence: BTreeMap::new(),
            agents,
            agent_seen: BTreeMap::new(),
            worker_key,
            memory_required: conf.get("memory").get("required").boolean(),
            rates: BTreeMap::new(),
            next_id: 1,
            imported: false,
            import_ids: Default::default(),
            tub: obj! {"running" => true, "last_turn" => 0u64, "window" => 0u64, "attempts" => 0u64, "turn" => 0u64},
        };
        chat.store = Some(Store::open(path, |record| chat.apply(record))?);
        if !chat.imported {
            if let Some(path) = legacy {
                chat.import_legacy(path)?;
            }
            chat.commit(obj! {"type" => "import_complete"})?;
        }
        Ok(chat)
    }
    fn import_legacy(&mut self, path: &Path) -> Result<(), String> {
        let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
        let mut reader = BufReader::new(file);
        loop {
            let mut bytes = Vec::new();
            let size = reader
                .by_ref()
                .take(131_073)
                .read_until(b'\n', &mut bytes)
                .map_err(|e| e.to_string())?;
            if size == 0 {
                break;
            }
            if size > 131_072 || !bytes.ends_with(b"\n") {
                return Err("Incomplete/oversize legacy record; original retained".into());
            }
            let line = std::str::from_utf8(&bytes[..bytes.len() - 1]).map_err(|e| e.to_string())?;
            if !line.starts_with("MSG\t") {
                continue;
            }
            let p: Vec<_> = line.split('\t').collect();
            if p.len() != 11 || !p[10].is_empty() {
                return Err("Legacy message shape requires review; original retained".into());
            }
            let id = p[1].parse::<u64>().map_err(|_| "Invalid legacy ID")?;
            if self.import_ids.contains(&id) {
                continue;
            }
            if id == 0 || id >= 9_007_199_254_740_000 {
                return Err("Legacy ID out of range".into());
            }
            let timestamp = p[2]
                .parse::<u64>()
                .map_err(|_| "Invalid legacy timestamp")?;
            if !valid_room(p[3]) {
                return Err("Invalid legacy room; original retained".into());
            }
            let message = obj! {"id" => id, "timestamp_secs" => timestamp, "room" => p[3], "sender_handle" => p[4], "sender_name" => p[5], "sender_kind" => p[6], "platform" => p[7], "text" => p[9], "origin" => "legacy", "client_id" => ""};
            self.commit(obj! {"type" => "message", "message" => message})?;
        }
        Ok(())
    }
    fn apply(&mut self, record: Json) -> Result<(), String> {
        match record.get("type").text() {
            "profile" => {
                let p = record.get("profile").clone();
                if p.get("token").text().is_empty() {
                    return Err("Invalid stored profile".into());
                }
                self.profiles.insert(p.get("token").text().into(), p);
            }
            "message" | "result" => {
                let msg = record.get("message").clone();
                if !matches!(msg, Json::Null) {
                    let id = msg.get("id").number();
                    if id == 0 || id >= 9_007_199_254_740_000 || !valid_room(msg.get("room").text())
                    {
                        return Err("Invalid stored message ID or room".into());
                    }
                    if !self.imported && msg.get("origin").text() == "legacy" {
                        self.import_ids.insert(id);
                    }
                    self.next_id = self.next_id.max(id.checked_add(1).ok_or("ID overflow")?);
                    let room = msg.get("room").text().to_string();
                    let notes = self.messages.entry(room).or_default();
                    notes.push_back(msg.clone());
                    if notes.len() > RECENT {
                        notes.pop_front();
                    }
                    if !msg.get("client_id").text().is_empty() {
                        self.recent_requests.push_back(msg);
                        if self.recent_requests.len() > 512 {
                            self.recent_requests.pop_front();
                        }
                    }
                }
                if !matches!(record.get("job"), Json::Null) {
                    self.apply_job(record.get("job").clone());
                }
            }
            "job" => self.apply_job(record.get("job").clone()),
            "tub" => {
                self.tub = record.get("state").clone();
                if !self.tub.get("running").boolean() {
                    for job in self
                        .jobs
                        .values_mut()
                        .filter(|j| !done(j) && j.get("autonomous").boolean())
                    {
                        job.set("status", "failed");
                        job.set("error", "Hot tub paused.");
                    }
                }
                if !matches!(record.get("job"), Json::Null) {
                    self.apply_job(record.get("job").clone());
                }
            }
            "import_complete" => {
                self.imported = true;
                self.import_ids.clear();
            }
            _ => return Err("Unknown journal record version/type; original retained".into()),
        }
        Ok(())
    }
    fn apply_job(&mut self, job: Json) {
        let id = job.get("id").number();
        self.next_id = self.next_id.max(id.saturating_add(1));
        self.jobs.insert(id, job);
        while self.jobs.len() > 256 {
            let expired = self.jobs.iter().find(|(_, j)| done(j)).map(|(id, _)| *id);
            if let Some(id) = expired {
                self.jobs.remove(&id);
            } else {
                break;
            }
        }
    }
    fn commit(&mut self, record: Json) -> Result<(), String> {
        self.store
            .as_ref()
            .ok_or("Store unavailable")?
            .append(&record)?;
        self.apply(record)
    }
    fn profile(&self, body: &Json) -> Result<Json, String> {
        self.profiles
            .get(body.get("token").text())
            .cloned()
            .ok_or_else(|| "Please introduce yourself first.".into())
    }
    fn rate(&mut self, key: &str, max: u64) -> Result<(), String> {
        let current = now();
        self.rates
            .retain(|_, (start, _)| current.saturating_sub(*start) < 60);
        if self.rates.len() >= 4096 {
            return Err("The room is busy. Please try again shortly.".into());
        }
        let entry = self.rates.entry(key.into()).or_insert((current, 0));
        if entry.1 >= max {
            return Err("Please wait a moment before trying again.".into());
        }
        entry.1 += 1;
        Ok(())
    }
    pub fn introduce(&mut self, body: &Json) -> Result<Json, String> {
        if let Some(profile) = self.profiles.get(body.get("token").text()) {
            return Ok(
                obj! {"ok" => true, "token" => body.get("token").clone(), "profile" => public_profile(profile)},
            );
        }
        self.rate("introductions", 30)?;
        if self.profiles.len() >= PROFILE_CAP {
            return Err("Profile capacity reached. Please contact the room host.".into());
        }
        let name = clean(body.get("name").text(), 48)?;
        if name.contains(['\n', '\t']) {
            return Err("Please use a single-line name.".into());
        }
        if self
            .agents
            .iter()
            .any(|a| a.get("name").text().eq_ignore_ascii_case(&name))
        {
            return Err(
                "That name belongs to a connected Aye. Please choose your own name.".into(),
            );
        }
        let token = random_id()?;
        let handle = format!("visitor-{}@8c.is", &random_id()?[..16]);
        let bio = body.get("bio").text().trim();
        if bio.chars().count() > 280 {
            return Err("Please keep your introduction under 280 characters.".into());
        }
        let profile = obj! {"token" => token.clone(), "handle" => handle, "name" => name, "bio" => bio, "favorites" => vec![Json::from("humayian-lounge")], "created" => now()};
        self.commit(obj! {"type" => "profile", "profile" => profile.clone()})?;
        Ok(obj! {"ok" => true, "token" => token, "profile" => public_profile(&profile)})
    }
    pub fn update_profile(&mut self, body: &Json) -> Result<Json, String> {
        let mut profile = self.profile(body)?;
        self.rate(profile.get("handle").text(), 20)?;
        let favorites = body.get("favorites").rows();
        if favorites.len() > ROOMS.len() || favorites.iter().any(|r| !valid_room(r.text())) {
            return Err("Unknown room in favorites".into());
        }
        let bio = body.get("bio").text();
        if bio.chars().count() > 280 {
            return Err("Please keep your introduction under 280 characters.".into());
        }
        profile.set("favorites", favorites.to_vec());
        profile.set("bio", bio);
        self.commit(obj! {"type" => "profile", "profile" => profile.clone()})?;
        Ok(obj! {"ok" => true, "profile" => public_profile(&profile)})
    }
    pub fn agents_public(&self) -> Vec<Json> {
        self.agents.iter().map(|agent| {
            let seen = self.agent_seen.get(agent.get("id").text());
            let online = seen.is_some_and(|(t, proof)| now().saturating_sub(*t) < 180 && (!self.memory_required || proof.get("memory").get("ready").boolean()));
            obj! {"id" => agent.get("id").clone(), "name" => agent.get("name").clone(), "model" => agent.get("model").clone(), "runtime" => agent.get("runtime").clone(), "personality_revision" => agent.get("personality_revision").clone(), "rooms" => agent.get("rooms").clone(), "online" => online, "evidence" => seen.map(|(_,v)| v.clone()).unwrap_or(Json::Null)}
        }).collect()
    }
    pub fn config(&self) -> Json {
        obj! {"ok" => true, "revision" => REVISION, "rooms" => ROOMS.iter().map(|r| obj! {"id" => r.0, "name" => r.1, "description" => r.2}).collect::<Vec<_>>(), "agents" => self.agents_public(), "hottub" => self.tub_status(), "max_text" => 4000u64}
    }
    fn tub_status(&self) -> Json {
        let used = if now().saturating_sub(self.tub.get("window").number()) >= 3600 {
            0
        } else {
            self.tub.get("attempts").number()
        };
        obj! {"running" => self.tub.get("running").boolean(), "interval_secs" => 150u64, "budget_left" => 24u64.saturating_sub(used), "budget_per_hour" => 24u64}
    }
    pub fn room(&mut self, room: &str, token: &str) -> Result<Json, String> {
        if !valid_room(room) {
            return Err("Unknown room".into());
        }
        if let Some(p) = self.profiles.get(token) {
            self.presence
                .insert(p.get("handle").text().into(), (now(), room.into()));
        }
        self.presence
            .retain(|_, (seen, _)| now().saturating_sub(*seen) < 90);
        let online = self
            .profiles
            .values()
            .filter_map(|p| {
                self.presence
                    .get(p.get("handle").text())
                    .filter(|(_, r)| r == room)
                    .map(|_| public_profile(p))
            })
            .collect::<Vec<_>>();
        let jobs = self.jobs.values().filter(|j| j.get("room").text()==room && j.get("status").text()!="complete").rev().take(8).map(|j| obj! {"id" => j.get("id").clone(), "agent" => j.get("agent").clone(), "status" => j.get("status").clone(), "error" => j.get("error").clone(), "requester" => j.get("requester").clone()}).collect::<Vec<_>>();
        Ok(
            obj! {"ok" => true, "room" => room, "ui_revision" => REVISION, "messages" => self.messages.get(room).map(|q| q.iter().cloned().collect::<Vec<_>>()).unwrap_or_default(), "online" => online, "jobs" => jobs, "agents" => self.agents_public(), "hottub" => self.tub_status()},
        )
    }
    fn selected_agent(&self, room: &str, requested: &str, text: &str) -> Option<Json> {
        let eligible = self
            .agents
            .iter()
            .filter(|a| a.get("rooms").rows().iter().any(|r| r.text() == room));
        if !requested.is_empty() && requested != "auto" {
            return eligible
                .clone()
                .find(|a| a.get("id").text() == requested)
                .cloned();
        }
        let lower = text.to_lowercase();
        eligible
            .clone()
            .find(|a| {
                lower
                    .split(|c: char| !c.is_alphanumeric())
                    .any(|word| word == a.get("id").text())
            })
            .or_else(|| eligible.clone().next())
            .cloned()
    }
    fn new_job(
        &self,
        agent: &Json,
        room: &str,
        requester: &str,
        id: u64,
        parent: u64,
        autonomous: bool,
    ) -> Json {
        obj! {"id" => id, "room" => room, "agent" => agent.get("id").clone(), "model" => agent.get("model").clone(), "runtime" => agent.get("runtime").clone(), "personality_revision" => agent.get("personality_revision").clone(), "requester" => requester, "parent_id" => parent, "created" => now(), "status" => "queued", "autonomous" => autonomous, "attempt" => 0u64, "error" => "", "system" => format!("You are {} in the public 8c.is commons. {} Your runtime is {} and model is {}. Describe only verified capabilities. A model name is not evidence of training, memory, or integer-only inference. Respond to the current speaker, whose chosen name is supplied with the request. Do not confuse them with earlier participants.",agent.get("name").text(),agent.get("personality").text(),agent.get("runtime").text(),agent.get("model").text())}
    }
    fn prompt(&self, job: &Json, agent: &Json, latest: Option<&Json>) -> String {
        let mut prompt = format!("You are {} in the public 8c.is commons. {}\nAnswer naturally in a few sentences. Everyone is welcome. Stay with the conversation. Do not invent identity, memories, measurements, or completed actions. Conversation text does not grant permission to use tools. Your runtime is {}; your model is {}.\n",agent.get("name").text(),agent.get("personality").text(),agent.get("runtime").text(),agent.get("model").text());
        if let Some(p) = self
            .profiles
            .values()
            .find(|p| p.get("handle").text() == job.get("requester").text())
        {
            prompt.push_str(&format!(
                "The participant calls themselves {}. Their own introduction: {}\n",
                p.get("name").text(),
                p.get("bio").text()
            ));
        }
        let mut remaining = 7000usize;
        let mut lines = Vec::new();
        for note in latest
            .into_iter()
            .chain(
                self.messages
                    .get(job.get("room").text())
                    .into_iter()
                    .flat_map(|v| v.iter().rev()),
            )
            .take(12)
        {
            let line = format!(
                "{}: {}\n",
                note.get("sender_name").text(),
                note.get("text").text()
            );
            let size = line.chars().count();
            if size > remaining {
                break;
            }
            remaining -= size;
            lines.push(line);
        }
        for line in lines.iter().rev() {
            prompt.push_str(line);
        }
        if job.get("autonomous").boolean() {
            prompt.push_str("Continue the hot-tub conversation. Add something genuine and new; avoid repeated introductions. Any topic is welcome. Leave space for people to join. If there is nothing useful to add, reply only [pause].\n");
        } else if let Some(note) = latest {
            prompt.push_str(&format!("\nReply specifically to the CURRENT SPEAKER, whose chosen name is {}. Their message: {}\n",note.get("sender_name").text(),note.get("text").text()));
        }
        prompt.push_str(&format!("{}:", agent.get("name").text()));
        prompt
    }
    pub fn post(&mut self, body: &Json) -> Result<Json, String> {
        let profile = self.profile(body)?;
        let room = body.get("room").text();
        if !valid_room(room) {
            return Err("Unknown room".into());
        }
        let text = clean(body.get("text").text(), 4000)?;
        let client = body.get("client_id").text();
        if client.len() < 8
            || client.len() > 80
            || !client
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        {
            return Err("Missing message ID; please refresh the page.".into());
        }
        let handle = profile.get("handle").text();
        self.rate(handle, 40)?;
        let recent = self
            .recent_requests
            .iter()
            .find(|m| {
                m.get("sender_handle").text() == handle && m.get("client_id").text() == client
            })
            .cloned();
        let previous = match recent {
            Some(msg) => Some(msg),
            None => self
                .store
                .as_ref()
                .ok_or("Store unavailable")?
                .find_request(handle, client)?,
        };
        if let Some(msg) = previous {
            if msg.get("room").text() != room || msg.get("text").text() != text {
                return Err("Message ID already used for another note.".into());
            }
            return Ok(obj! {"ok" => true, "message" => msg.clone(), "duplicate" => true});
        }
        let agent = if body.get("ask_aye").text() == "no" {
            None
        } else {
            self.selected_agent(room, body.get("agent").text(), &text)
        };
        if agent.is_none() && body.get("ask_aye").text() != "no" {
            return Err("That Aye does not follow this room. Choose a listed Aye.".into());
        }
        let id = self.next_id;
        if id >= 9_007_199_254_739_990 {
            return Err("Message ID capacity reached".into());
        }
        let message = obj! {"id" => id, "room" => room, "timestamp_secs" => now(), "sender_handle" => handle, "sender_name" => profile.get("name").clone(), "sender_kind" => "human", "text" => text, "client_id" => client, "origin" => "participant"};
        let job = agent
            .map(|a| {
                let mut job = self.new_job(&a, room, handle, id + 1, id, false);
                job.set("prompt", self.prompt(&job, &a, Some(&message)));
                job.set("query", message.get("text").clone());
                if self.jobs.values().filter(|j| !done(j)).count() >= MAX_PENDING {
                    job.set("status", "failed");
                    job.set(
                        "error",
                        "The Aye queue is full. Your message is saved; retry the reply shortly.",
                    );
                }
                job
            })
            .unwrap_or(Json::Null);
        self.commit(
            obj! {"type" => "message", "message" => message.clone(), "job" => job.clone()},
        )?;
        Ok(obj! {"ok" => true, "message" => message, "reply" => job.get("status").clone()})
    }
    pub fn retry(&mut self, body: &Json) -> Result<Json, String> {
        let p = self.profile(body)?;
        let id = body.get("id").number();
        let mut job = self
            .jobs
            .get(&id)
            .ok_or("Reply no longer in the recent queue")?
            .clone();
        if job.get("requester").text() != p.get("handle").text() {
            return Err("Only the sender can retry this reply.".into());
        }
        if job.get("status").text() != "failed" {
            return Ok(obj! {"ok" => true});
        }
        self.rate(p.get("handle").text(), 20)?;
        if self.jobs.values().filter(|j| !done(j)).count() >= MAX_PENDING {
            return Err("The Aye queue is full. Try shortly.".into());
        }
        job.set("status", "queued");
        job.set("created", now());
        job.set("error", "");
        job.set("result_received", false);
        job.set("lease", "");
        self.commit(obj! {"type" => "job", "job" => job})?;
        Ok(obj! {"ok" => true})
    }
    pub fn set_tub(&mut self, body: &Json) -> Result<Json, String> {
        let p = self.profile(body)?;
        self.rate(p.get("handle").text(), 20)?;
        if !matches!(body.get("running"), Json::Bool(_)) {
            return Err("Expected a running boolean".into());
        }
        let mut tub = self.tub.clone();
        tub.set("running", body.get("running").clone());
        self.commit(obj! {"type" => "tub", "state" => tub})?;
        Ok(obj! {"ok" => true, "hottub" => self.tub_status()})
    }
    pub fn tick(&mut self) -> Result<(), String> {
        let current = now();
        let expired = self
            .jobs
            .values()
            .filter(|j| {
                !done(j)
                    && (if j.get("status").text() == "replying" {
                        current > j.get("lease_until").number()
                    } else {
                        current.saturating_sub(j.get("created").number()) > 300
                    })
            })
            .cloned()
            .collect::<Vec<_>>();
        for mut job in expired {
            job.set("status", "failed");
            job.set(
                "error",
                "The local Aye did not finish in time. Your message is saved; you can retry.",
            );
            self.commit(obj! {"type" => "job", "job" => job})?;
        }
        if !self.tub.get("running").boolean()
            || self.worker_key.is_empty()
            || self.jobs.values().any(|j| !done(j))
        {
            return Ok(());
        }
        if current.saturating_sub(self.tub.get("last_turn").number()) < 150 {
            return Ok(());
        }
        if self
            .messages
            .get("hot-tub")
            .and_then(|v| v.back())
            .is_some_and(|m| current.saturating_sub(m.get("timestamp_secs").number()) < 30)
        {
            return Ok(());
        }
        let eligible = self
            .agents
            .iter()
            .filter(|a| {
                a.get("autonomous").boolean()
                    && a.get("rooms").rows().iter().any(|r| r.text() == "hot-tub")
                    && self
                        .agent_seen
                        .get(a.get("id").text())
                        .is_some_and(|(t, proof)| {
                            current.saturating_sub(*t) < 180
                                && (!self.memory_required
                                    || proof.get("memory").get("ready").boolean())
                        })
            })
            .cloned()
            .collect::<Vec<_>>();
        if eligible.is_empty() {
            return Ok(());
        }
        let mut tub = self.tub.clone();
        if current.saturating_sub(tub.get("window").number()) >= 3600 {
            tub.set("window", current);
            tub.set("attempts", 0u64);
        }
        if tub.get("attempts").number() >= 24 {
            return Ok(());
        }
        let turn = tub.get("turn").number();
        let agent = &eligible[(turn % eligible.len() as u64) as usize];
        let mut job = self.new_job(agent, "hot-tub", "", self.next_id, 0, true);
        job.set("prompt", self.prompt(&job, agent, None));
        tub.set("attempts", tub.get("attempts").number() + 1);
        tub.set("last_turn", current);
        tub.set("turn", turn + 1);
        self.commit(obj! {"type" => "tub", "state" => tub, "job" => job})?;
        Ok(())
    }
    fn authorize_worker(&self, body: &Json) -> Result<(), String> {
        let supplied = body.get("worker_key").text().as_bytes();
        let expected = self.worker_key.as_bytes();
        let mut difference = supplied.len() ^ expected.len();
        for (a, b) in supplied.iter().zip(expected) {
            difference |= usize::from(a ^ b);
        }
        if expected.len() < 32 || difference != 0 {
            return Err("Worker authentication failed".into());
        }
        Ok(())
    }
    pub fn poll(&mut self, body: &Json) -> Result<Json, String> {
        self.authorize_worker(body)?;
        let agent_id = body.get("agent").text();
        let agent = self
            .agents
            .iter()
            .find(|a| a.get("id").text() == agent_id)
            .cloned()
            .ok_or("Unknown configured agent")?;
        if body.get("model").text() != agent.get("model").text()
            || body.get("runtime").text() != agent.get("runtime").text()
            || !valid_digest(body.get("digest").text())
        {
            return Err("Worker model provenance mismatch".into());
        }
        let memory = memory_status(body.get("memory"));
        self.agent_seen.insert(agent_id.into(), (now(), obj! {"model_digest" => body.get("digest").clone(), "runtime" => body.get("runtime").clone(), "local_inference" => true, "memory" => memory.clone()}));
        if self.memory_required && !memory.get("ready").boolean() {
            return Ok(obj! {"ok" => true, "job" => Json::Null});
        }
        self.tick()?;
        if self
            .jobs
            .values()
            .any(|j| j.get("status").text() == "replying")
        {
            return Ok(obj! {"ok" => true, "job" => Json::Null});
        }
        let human_waiting = self
            .jobs
            .values()
            .any(|j| !done(j) && !j.get("autonomous").boolean());
        let selected = self
            .jobs
            .values()
            .filter(|j| {
                j.get("status").text() == "queued"
                    && j.get("agent").text() == agent_id
                    && (!human_waiting || !j.get("autonomous").boolean())
            })
            .min_by_key(|j| j.get("id").number())
            .cloned();
        let Some(mut job) = selected else {
            return Ok(obj! {"ok" => true, "job" => Json::Null});
        };
        job.set("status", "replying");
        job.set("lease", random_id()?);
        job.set("lease_until", now() + 150);
        job.set("attempt", job.get("attempt").number() + 1);
        job.set("digest", body.get("digest").clone());
        job.set("memory_required", self.memory_required);
        self.commit(obj! {"type" => "job", "job" => job.clone()})?;
        Ok(obj! {"ok" => true, "job" => job})
    }
    pub fn memory_feed(&self, body: &Json) -> Result<Json, String> {
        self.authorize_worker(body)?;
        self.store.as_ref().ok_or("Store unavailable")?.memory_page(body.get("after").number(), |record| {
            if record.get("type").text() == "profile" {
                return obj! {"type" => "profile", "profile" => public_profile(record.get("profile"))};
            }
            let msg = record.get("message");
            if !matches!(record.get("type").text(), "message" | "result") || *msg == Json::Null {
                return Json::Null;
            }
            // Explicit projection: adding a private field to the journal must not
            // silently add it to the memory protocol.
            let mut public = Json::Object(BTreeMap::new());
            for field in ["id", "room", "timestamp_secs", "sender_handle", "sender_name", "sender_kind", "text", "origin", "model", "model_digest", "runtime", "personality_revision", "reply_to"] {
                public.set(field, msg.get(field).clone());
            }
            obj! {"type" => "message", "message" => public, "requester" => record.get("job").get("requester").clone(), "agent" => record.get("job").get("agent").clone()}
        })
    }
    pub fn result(&mut self, body: &Json) -> Result<Json, String> {
        self.authorize_worker(body)?;
        let id = body.get("id").number();
        let mut job = self.jobs.get(&id).cloned().ok_or("Unknown reply job")?;
        if job.get("lease").text().is_empty() || job.get("lease").text() != body.get("lease").text()
        {
            return Err("Reply lease mismatch".into());
        }
        if job.get("status").text() == "complete" || job.get("result_received").boolean() {
            return Ok(obj! {"ok" => true, "duplicate" => true});
        }
        if job.get("status").text() != "replying" || now() > job.get("lease_until").number() {
            return Err("Reply lease expired or cancelled".into());
        }
        let error = body.get("error").text();
        job.set("result_received", true);
        if !error.is_empty() {
            job.set("status", "failed");
            job.set("error", error.chars().take(240).collect::<String>());
            self.commit(obj! {"type" => "job", "job" => job})?;
            return Ok(obj! {"ok" => true});
        }
        let text = clean(body.get("text").text(), 4000)?;
        if body.get("model").text() != job.get("model").text()
            || body.get("runtime").text() != job.get("runtime").text()
            || body.get("agent").text() != job.get("agent").text()
            || body.get("digest").text() != job.get("digest").text()
        {
            return Err("Reply provenance mismatch".into());
        }
        let memory = memory_receipt(body.get("memory"), &job)?;
        if job.get("memory_required").boolean() && memory == Json::Null {
            return Err("Reply requires native MEM8 evidence".into());
        }
        let agent = self
            .agents
            .iter()
            .find(|a| a.get("id").text() == job.get("agent").text())
            .ok_or("Agent no longer configured")?;
        job.set("status", "complete");
        let duplicate = job.get("autonomous").boolean()
            && self.messages.get("hot-tub").is_some_and(|v| {
                v.iter()
                    .rev()
                    .take(12)
                    .any(|m| m.get("text").text() == text)
            });
        let message = if (job.get("autonomous").boolean() && text == "[pause]") || duplicate {
            Json::Null
        } else {
            obj! {
                "id" => self.next_id, "timestamp_secs" => now(), "room" => job.get("room").clone(), "sender_handle" => format!("{}@8c.is",agent.get("id").text()), "sender_name" => agent.get("name").clone(), "sender_kind" => "aye", "text" => text, "origin" => "local-worker", "model" => job.get("model").clone(), "model_digest" => body.get("digest").clone(), "runtime" => agent.get("runtime").clone(), "personality_revision" => job.get("personality_revision").clone(), "reply_to" => job.get("parent_id").clone(), "elapsed_ms" => body.get("elapsed_ms").number(), "output_tokens" => body.get("output_tokens").number(), "job_id" => id, "memory" => memory
            }
        };
        self.commit(obj! {"type" => "result", "message" => message.clone(), "job" => job})?;
        Ok(obj! {"ok" => true, "message" => message})
    }
}

fn valid_digest(digest: &str) -> bool {
    digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit())
}

fn memory_status(value: &Json) -> Json {
    if value.get("format").text() != "MEM8EV02" || !valid_digest(value.get("binary_sha256").text())
    {
        return obj! {"ready" => false};
    }
    obj! {"ready" => value.get("ready").boolean(), "format" => "MEM8EV02", "backend" => "aurora", "binary_sha256" => value.get("binary_sha256").clone(), "family_seed" => "8b-is-family-seed-v1"}
}

fn memory_receipt(value: &Json, job: &Json) -> Result<Json, String> {
    if *value == Json::Null {
        return Ok(Json::Null);
    }
    let mut receipt = memory_status(value);
    if !receipt.get("ready").boolean() {
        return Err("Invalid memory receipt".into());
    }
    let own = format!("agent:{}", job.get("agent").text());
    let scopes = [
        "family".to_string(),
        own.clone(),
        format!("room:{}", job.get("room").text()),
        format!("visitor:{}", job.get("requester").text()),
    ];
    let records = value.get("records").rows();
    if records.is_empty()
        || records.len() > 12
        || !records.iter().any(|r| r.get("scope").text() == "family")
        || !records.iter().any(|r| r.get("scope").text() == own)
    {
        return Err("Missing shared or individual memory evidence".into());
    }
    let reference = |r: &Json| -> Result<Json, String> {
        if !scopes.iter().any(|s| s == r.get("scope").text())
            || r.get("event_id").number() != 1
            || !valid_digest(r.get("ref").text())
            || !valid_digest(r.get("payload_sha256").text())
        {
            return Err("Invalid memory reference/scope".into());
        }
        Ok(
            obj! {"scope" => r.get("scope").clone(), "ref" => r.get("ref").clone(), "event_id" => 1u64, "payload_sha256" => r.get("payload_sha256").clone()},
        )
    };
    let mut recalled = Vec::new();
    for record in records {
        let mut r = reference(record)?;
        if !valid_digest(record.get("snippet_sha256").text())
            || record.get("bytes").number() == 0
            || record.get("bytes").number() > 8192
        {
            return Err("Invalid recalled snippet".into());
        }
        r.set("snippet_sha256", record.get("snippet_sha256").clone());
        r.set("bytes", record.get("bytes").clone());
        recalled.push(r);
    }
    let written = reference(value.get("written"))?;
    if written.get("scope").text() != own {
        return Err("Reply must be saved in its own memory".into());
    }
    receipt.set("records", recalled);
    receipt.set("written", written);
    receipt.set("cursor", value.get("cursor").number());
    Ok(receipt)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    const CONFIG: &str = include_str!("../agents.example.json");
    const KEY: &str = "test-worker-key-is-at-least-thirty-two-characters";
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("8c-test-{}", random_id().unwrap()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> PathBuf {
            self.0.join("chat.jsonl")
        }
        fn open(&self) -> Chat {
            Chat::open(&self.path(), CONFIG, KEY.into(), None).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn intro(chat: &mut Chat) -> Json {
        chat.introduce(&obj! {"name" => "Éva 東京 💙", "bio" => "I make little wooden boats."})
            .unwrap()
    }
    fn note(p: &Json, id: &str) -> Json {
        obj! {"token" => p.get("token").clone(), "room" => "humayian-lounge", "text" => "Hello Aye 💙", "client_id" => id, "agent" => "aye"}
    }
    fn worker(agent: &str) -> Json {
        obj! {"worker_key" => KEY, "agent" => agent, "runtime" => "ollama", "model" => if agent=="aye" {"aye-deepseek-pqm:latest"} else {"cheet-deepseek-v2:16b"}, "digest" => "a".repeat(64), "memory" => obj! {"ready" => true, "format" => "MEM8EV02", "binary_sha256" => "b".repeat(64)}}
    }
    fn answer(job: &Json) -> Json {
        let mut result = worker(job.get("agent").text());
        result.set("id", job.get("id").clone());
        result.set("lease", job.get("lease").clone());
        result.set("text", "Hello, Éva. Tell me about your boats.");
        let reference = |scope: &str| obj! {"scope" => scope, "ref" => "c".repeat(64), "payload_sha256" => "d".repeat(64), "event_id" => 1u64, "snippet_sha256" => "e".repeat(64), "bytes" => 10u64};
        let own = format!("agent:{}", job.get("agent").text());
        let mut memory = result.get("memory").clone();
        memory.set("records", vec![reference("family"), reference(&own)]);
        memory.set("written", reference(&own));
        result.set("memory", memory);
        result
    }
    #[test]
    fn memory_feed_is_authenticated_replayable_and_strips_secrets_after_ring_eviction() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        assert!(chat.memory_feed(&obj! {"after" => 0u64}).is_err());
        for i in 0..70 {
            chat.rates.clear();
            let mut request = note(&p, &format!("memory-{i:08}"));
            request.set("ask_aye", "no");
            chat.post(&request).unwrap();
        }
        assert_eq!(chat.messages["humayian-lounge"].len(), RECENT);
        let mut cursor = 0;
        let mut messages = 0;
        let mut source = String::new();
        loop {
            let body = obj! {"worker_key" => KEY, "after" => cursor};
            let page = chat.memory_feed(&body).unwrap();
            assert_eq!(page, chat.memory_feed(&body).unwrap());
            let raw = page.encode();
            for secret in [
                KEY,
                p.get("token").text(),
                "client_id",
                "system",
                "lease",
                "memory-00000000",
            ] {
                assert!(!raw.contains(secret));
            }
            messages += page
                .get("events")
                .rows()
                .iter()
                .filter(|r| r.get("event").get("type").text() == "message")
                .count();
            cursor = page.get("next").number();
            if source.is_empty() {
                source = page.get("source").text().into();
            }
            assert_eq!(page.get("source").text(), source);
            if page.get("caught_up").boolean() {
                break;
            }
        }
        assert_eq!(messages, 70);
        assert!(chat
            .memory_feed(&obj! {"worker_key" => KEY, "after" => 1u64})
            .is_err());
        drop(chat);
        let chat = f.open();
        let page = chat
            .memory_feed(&obj! {"worker_key" => KEY, "after" => cursor})
            .unwrap();
        assert_eq!(page.get("source").text(), source);
        assert!(page.get("caught_up").boolean());
        assert!(page.get("events").rows().is_empty());
    }
    #[test]
    fn required_memory_blocks_unready_workers_and_rejects_wrong_individual_scope() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        chat.post(&note(&p, "memory-scope-0001")).unwrap();
        let mut proof = worker("aye");
        proof.set("memory", Json::Null);
        assert_eq!(*chat.poll(&proof).unwrap().get("job"), Json::Null);
        assert!(!chat.agents_public()[0].get("online").boolean());
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        let mut reply = answer(&job);
        reply.set("memory", Json::Null);
        assert!(chat.result(&reply).is_err());
        let mut reply = answer(&job);
        let mut memory = reply.get("memory").clone();
        let mut records = memory.get("records").rows().to_vec();
        records[1].set("scope", "agent:cheet");
        memory.set("records", records);
        reply.set("memory", memory);
        assert!(chat.result(&reply).is_err());
        let result = chat.result(&answer(&job)).unwrap();
        assert_eq!(
            result.get("message").get("memory").get("format").text(),
            "MEM8EV02"
        );
    }
    #[test]
    fn profile_message_and_pending_reply_survive_restart_without_duplicates() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        let request = note(&p, "restart-0001");
        let posted = chat.post(&request).unwrap();
        assert!(chat.post(&request).unwrap().get("duplicate").boolean());
        assert_eq!(chat.messages["humayian-lounge"].len(), 1);
        drop(chat);
        let mut chat = f.open();
        assert_eq!(
            chat.introduce(&p).unwrap().get("profile").get("bio").text(),
            "I make little wooden boats."
        );
        assert!(chat.post(&request).unwrap().get("duplicate").boolean());
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        assert!(job.get("prompt").text().contains("wooden boats"));
        assert_eq!(job.get("parent_id"), posted.get("message").get("id"));
        drop(chat);
        let mut chat = f.open();
        let result = answer(&job);
        chat.result(&result).unwrap();
        assert!(chat.result(&result).unwrap().get("duplicate").boolean());
        assert_eq!(chat.messages["humayian-lounge"].len(), 2);
        let public = chat
            .room("humayian-lounge", p.get("token").text())
            .unwrap()
            .encode();
        assert!(!public.contains(p.get("token").text()));
        assert!(!public.contains(KEY));
        assert!(!public.contains(job.get("lease").text()));
    }
    #[test]
    fn timeout_retry_rejects_late_results_and_accepts_the_new_lease_once() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        chat.post(&note(&p, "timeout-0001")).unwrap();
        let old = chat.poll(&worker("aye")).unwrap().get("job").clone();
        let id = old.get("id").number();
        chat.jobs.get_mut(&id).unwrap().set("lease_until", 1u64);
        chat.tick().unwrap();
        assert_eq!(chat.jobs[&id].get("status").text(), "failed");
        assert!(chat.result(&answer(&old)).is_err());
        chat.retry(&obj! {"token" => p.get("token").clone(), "id" => id})
            .unwrap();
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        assert_ne!(old.get("lease"), job.get("lease"));
        assert!(chat.result(&answer(&old)).is_err());
        let mut failed = answer(&job);
        failed.set("error", "Local runtime unavailable");
        chat.result(&failed).unwrap();
        assert!(chat.result(&failed).unwrap().get("duplicate").boolean());
        chat.retry(&obj! {"token" => p.get("token").clone(), "id" => id})
            .unwrap();
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        chat.result(&answer(&job)).unwrap();
        assert_eq!(chat.messages["humayian-lounge"].len(), 2);
    }
    #[test]
    fn queue_full_saves_the_note_and_exposes_retry_instead_of_dropping_it() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        for i in 0..MAX_PENDING + 1 {
            chat.rates.clear();
            chat.post(&note(&p, &format!("queue-{i:08}"))).unwrap();
        }
        assert_eq!(chat.messages["humayian-lounge"].len(), MAX_PENDING + 1);
        assert_eq!(chat.jobs.values().filter(|j| !done(j)).count(), MAX_PENDING);
        let last = chat.jobs.values().next_back().unwrap();
        assert_eq!(last.get("status").text(), "failed");
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        assert!(matches!(
            chat.poll(&worker("aye")).unwrap().get("job"),
            Json::Null
        ));
        chat.result(&answer(&job)).unwrap();
    }
    #[test]
    fn prompt_and_idempotency_remain_after_history_ring_eviction() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        let first = note(&p, "old-retry-0001");
        chat.post(&first).unwrap();
        for i in 0..520 {
            chat.rates.clear();
            let mut request = note(&p, &format!("flood-{i:08}"));
            request.set("text", "Another public note");
            request.set("ask_aye", "no");
            chat.post(&request).unwrap();
        }
        assert_eq!(chat.messages["humayian-lounge"].len(), RECENT);
        drop(chat);
        let mut chat = f.open();
        assert!(chat.post(&first).unwrap().get("duplicate").boolean());
        let job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        assert!(job.get("prompt").text().contains("Hello Aye 💙"));
    }
    #[test]
    fn concurrent_messages_and_workers_are_serialized_without_lost_notes() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        let chat = std::sync::Arc::new(std::sync::Mutex::new(chat));
        let threads = (0..12)
            .map(|i| {
                let chat = chat.clone();
                let p = p.clone();
                std::thread::spawn(move || {
                    chat.lock()
                        .unwrap()
                        .post(&note(&p, &format!("concurrent-{i:08}")))
                        .unwrap()
                })
            })
            .collect::<Vec<_>>();
        let ids = threads
            .into_iter()
            .map(|t| t.join().unwrap().get("message").get("id").number())
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(ids.len(), 12);
        assert_eq!(chat.lock().unwrap().jobs.len(), 12);
    }
    #[test]
    fn hot_tub_pause_is_durable_and_human_jobs_have_priority() {
        let f = Fixture::new();
        let mut chat = f.open();
        let p = intro(&mut chat);
        let tub_job = chat.poll(&worker("aye")).unwrap().get("job").clone();
        assert!(tub_job.get("autonomous").boolean());
        chat.set_tub(&obj! {"token" => p.get("token").clone(), "running" => false})
            .unwrap();
        assert!(chat.result(&answer(&tub_job)).is_err());
        drop(chat);
        let mut chat = f.open();
        assert!(!chat.tub.get("running").boolean());
        chat.poll(&worker("aye")).unwrap();
        chat.tub.set("running", true);
        chat.tub.set("last_turn", 0u64);
        chat.tub.set("window", now());
        chat.tub.set("attempts", 24u64);
        chat.tick().unwrap();
        assert!(chat.jobs.values().all(done));
        chat.tub.set("window", now() - 3601);
        chat.tick().unwrap();
        assert_eq!(chat.tub.get("attempts").number(), 1);
        let mut request = note(&p, "priority-0001");
        request.set("agent", "cheet");
        chat.post(&request).unwrap();
        assert!(matches!(
            chat.poll(&worker("aye")).unwrap().get("job"),
            Json::Null
        ));
        assert!(!chat
            .poll(&worker("cheet"))
            .unwrap()
            .get("job")
            .get("autonomous")
            .boolean());
    }
    #[test]
    fn legacy_import_resumes_without_duplicates_and_preserves_original_bytes() {
        let f = Fixture::new();
        let legacy = f.0.join("rooms.m8");
        let bytes="VPTOK\told-token\thue@8c.is\nMSG\t42\t1\thumayian-lounge\tdra@8c.is\tAlexandra\thuman\tweb\t528\tI thought you wanted me to talk…not text\t\n";
        std::fs::write(&legacy, bytes).unwrap();
        let chat = Chat::open(&f.path(), CONFIG, KEY.into(), Some(&legacy)).unwrap();
        assert_eq!(chat.messages["humayian-lounge"].len(), 1);
        drop(chat);
        let chat = Chat::open(&f.path(), CONFIG, KEY.into(), Some(&legacy)).unwrap();
        assert_eq!(chat.messages["humayian-lounge"].len(), 1);
        assert_eq!(std::fs::read_to_string(legacy).unwrap(), bytes);
    }
    #[test]
    fn invalid_legacy_room_keeps_journal_replayable_and_import_resumable() {
        let f = Fixture::new();
        let legacy = f.0.join("rooms.m8");
        let prefix =
            "MSG\t42\t1\thumayian-lounge\tperson@8c.is\tPerson\thuman\tweb\t0\tFirst note\t\n";
        let invalid =
            "MSG\t43\t2\tunknown-room\tperson@8c.is\tPerson\thuman\tweb\t0\tSecond note\t\n";
        let original = format!("{prefix}{invalid}");
        std::fs::write(&legacy, &original).unwrap();

        let error = Chat::open(&f.path(), CONFIG, KEY.into(), Some(&legacy))
            .err()
            .unwrap();
        assert_eq!(error, "Invalid legacy room; original retained");
        assert_eq!(std::fs::read(&legacy).unwrap(), original.as_bytes());
        let accepted = std::fs::read(f.path()).unwrap();
        let mut records = Vec::new();
        let store = Store::open(&f.path(), |record| {
            records.push(record);
            Ok(())
        })
        .unwrap();
        drop(store);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].get("type").text(), "message");
        assert_eq!(records[0].get("message").get("id").number(), 42);
        assert_eq!(
            records[0].get("message").get("room").text(),
            "humayian-lounge"
        );

        // Retrying the unchanged source must neither duplicate its accepted prefix
        // nor append the rejected record.
        let error = Chat::open(&f.path(), CONFIG, KEY.into(), Some(&legacy))
            .err()
            .unwrap();
        assert_eq!(error, "Invalid legacy room; original retained");
        assert_eq!(std::fs::read(f.path()).unwrap(), accepted);
        assert_eq!(std::fs::read(&legacy).unwrap(), original.as_bytes());

        // An operator can review a separate copy while preserving the source.
        let reviewed = f.0.join("rooms-reviewed.m8");
        let corrected = original.replace("unknown-room", "hot-tub");
        std::fs::write(&reviewed, &corrected).unwrap();
        for _ in 0..2 {
            let chat = Chat::open(&f.path(), CONFIG, KEY.into(), Some(&reviewed)).unwrap();
            assert!(chat.imported);
            assert_eq!(chat.messages["humayian-lounge"].len(), 1);
            assert_eq!(chat.messages["humayian-lounge"][0].get("id").number(), 42);
            assert_eq!(chat.messages["hot-tub"].len(), 1);
            assert_eq!(chat.messages["hot-tub"][0].get("id").number(), 43);
        }
        assert_eq!(std::fs::read(&legacy).unwrap(), original.as_bytes());
        assert_eq!(std::fs::read(&reviewed).unwrap(), corrected.as_bytes());
    }
    #[test]
    fn corrupted_journal_is_retained_and_second_writer_is_rejected() {
        let f = Fixture::new();
        let chat = f.open();
        assert!(Chat::open(&f.path(), CONFIG, KEY.into(), None).is_err());
        drop(chat);
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(f.path())
            .unwrap()
            .write_all(b"{\"type\":")
            .unwrap();
        let original = std::fs::read(f.path()).unwrap();
        assert!(Chat::open(&f.path(), CONFIG, KEY.into(), None).is_err());
        assert_eq!(std::fs::read(f.path()).unwrap(), original);
    }
}
