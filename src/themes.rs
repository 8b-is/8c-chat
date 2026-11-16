//! Reviewed room presets, not executable model CSS/HTML/JavaScript.
//!
//! Theme revisions and timestamps are integer counters/seconds. Browser SVG/CSS
//! interpolation is an external rendering boundary; this module only chooses IDs.
use crate::{json::Json, obj};

pub const PRESETS: &[(&str, &str)] = &[
    ("none", "Classic ink"),
    ("qwave-whisper-v1", "Qwave Whisper"),
    ("violet-night-v1", "Violet Night"),
];
pub const COOLDOWN_SECS: u64 = 15;

pub fn label(preset: &str) -> Result<&'static str, String> {
    PRESETS
        .iter()
        .find(|(id, _)| *id == preset)
        .map(|(_, label)| *label)
        .ok_or_else(|| "Choose a reviewed room style.".into())
}

pub fn catalog() -> Json {
    PRESETS
        .iter()
        .map(|(id, label)| obj! {"id" => *id, "label" => *label})
        .collect::<Vec<_>>()
        .into()
}

pub fn capability() -> Json {
    obj! {"presets" => PRESETS.iter().map(|(id, _)| Json::from(*id)).collect::<Vec<_>>()}
}

pub fn initial() -> Json {
    obj! {"preset" => "none", "revision" => 0u64, "updated_at" => 0u64, "actor_name" => "", "actor_handle" => ""}
}

/// A deliberately conservative *intent hint*, not an NLP authorization claim.
/// Only the current human message is considered, never recalled history. Room
/// authority still comes from the authenticated human/leased job. Ambiguous,
/// quoted, negative and explanatory requests use the manual room-style picker.
/// This first version recognizes English requests; other languages use the picker.
pub fn requested(text: &str) -> bool {
    let lower = text.to_lowercase();
    if lower.contains(['`', '{', '[', '"', '“', '”'])
        || lower.trim_start().starts_with(['\'', '‘', '’'])
    {
        return false;
    }
    let words: Vec<_> = lower
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect();
    if words.iter().any(|w| {
        matches!(
            *w,
            "not"
                | "never"
                | "don"
                | "dont"
                | "no"
                | "shouldn"
                | "wouldn"
                | "couldn"
                | "mustn"
                | "avoid"
                | "unless"
                | "without"
                | "how"
                | "example"
                | "explain"
                | "code"
        )
    }) {
        return false;
    }
    let subject = words.iter().any(|w| {
        matches!(
            *w,
            "theme" | "themes" | "background" | "backgrounds" | "qwave"
        )
    });
    // Require a direct imperative/polite request at the start, not words
    // appearing somewhere in a question about a previous or hypothetical action.
    let mut request = words.as_slice();
    if request
        .first()
        .is_some_and(|w| matches!(*w, "aye" | "cheet" | "astra" | "agy" | "sol" | "grok"))
    {
        request = &request[1..];
    }
    if request.first() == Some(&"please") {
        request = &request[1..];
    }
    for prefix in [
        &["can", "you"][..],
        &["could", "you"],
        &["would", "you"],
        &["will", "you"],
        &["i", "want", "you", "to"],
        &["i", "would", "like", "you", "to"],
        &["i", "d", "like", "you", "to"],
    ] {
        if request.starts_with(prefix) {
            request = &request[prefix.len()..];
            break;
        }
    }
    if request.first() == Some(&"please") {
        request = &request[1..];
    }
    let change = request.first().is_some_and(|w| {
        matches!(
            *w,
            "change"
                | "set"
                | "make"
                | "switch"
                | "give"
                | "turn"
                | "enable"
                | "reset"
                | "restore"
                | "use"
                | "put"
                | "add"
                | "apply"
                | "remove"
                | "disable"
        )
    });
    subject && change
}

/// Reject extra keys rather than tolerating model-provided URLs, room IDs or code.
pub fn action(value: &Json) -> Result<&str, String> {
    let Json::Object(fields) = value else {
        return Err("Invalid room style action.".into());
    };
    if fields.len() != 1 || !fields.contains_key("preset") {
        return Err("Room style actions accept only a reviewed preset.".into());
    }
    let preset = value.get("preset").text();
    label(preset)?;
    Ok(preset)
}

pub fn revision(value: &Json) -> Result<u64, String> {
    let Json::Number(raw) = value else {
        return Err("Refresh the room style before changing it.".into());
    };
    if raw.is_empty() || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return Err("Invalid room style revision.".into());
    }
    raw.parse::<u64>()
        .ok()
        .filter(|n| *n < 9_007_199_254_740_000)
        .ok_or_else(|| "Invalid room style revision.".into())
}

/// Build the next immutable state before writing the journal. Matching presets
/// are idempotent no-ops. A stale model must never overwrite a more recent human.
pub fn change(
    current: &Json,
    preset: &str,
    expected: u64,
    actor_name: &str,
    actor_handle: &str,
    now: u64,
) -> Result<Json, String> {
    label(preset)?;
    let old_revision = revision(current.get("revision"))?;
    if old_revision != expected {
        return Err(
            "The room style changed while this request was pending. Refresh or ask again.".into(),
        );
    }
    if current.get("preset").text() == preset {
        return Ok(current.clone());
    }
    // Reset is always available, even during the cosmetic change cooldown.
    if preset != "none"
        && old_revision > 0
        && now.saturating_sub(current.get("updated_at").number()) < COOLDOWN_SECS
    {
        return Err("Give this room style a moment; try again in 15 seconds.".into());
    }
    let next = old_revision
        .checked_add(1)
        .filter(|n| *n < 9_007_199_254_740_000)
        .ok_or("Room style revision capacity reached.")?;
    Ok(
        obj! {"preset" => preset, "revision" => next, "updated_at" => now, "actor_name" => actor_name, "actor_handle" => actor_handle},
    )
}

/// Replay accepts only known fields and the next state. No-ops are not journaled.
/// Fields live inside existing message/result journal records, so older server
/// binaries can still replay the journal without inventing a new event type.
pub fn validate_stored(value: &Json, previous: &Json) -> Result<(), String> {
    let Json::Object(fields) = value else {
        return Err("Invalid stored room style".into());
    };
    if fields.len() != 5
        || ![
            "preset",
            "revision",
            "updated_at",
            "actor_name",
            "actor_handle",
        ]
        .iter()
        .all(|k| fields.contains_key(*k))
    {
        return Err("Invalid stored room style fields".into());
    }
    label(value.get("preset").text())?;
    let next = revision(value.get("revision"))?;
    revision(value.get("updated_at"))?;
    for field in ["actor_name", "actor_handle"] {
        if !matches!(value.get(field), Json::String(s) if !s.is_empty() && s.chars().count() <= 100 && !s.chars().any(char::is_control))
        {
            return Err("Invalid stored room style attribution".into());
        }
    }
    if next
        != revision(previous.get("revision"))?
            .checked_add(1)
            .ok_or("Style revision overflow")?
    {
        return Err("Invalid stored room style sequence".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn intent_uses_direct_non_explanatory_requests() {
        for text in [
            "Please give this room the qwave background",
            "Set the room theme to violet",
            "Reset the background",
            "Remove this background",
            "Aye, could you please set this room theme to Qwave Whisper?",
            "Sol, please set this room theme to violet",
            "Grok, could you reset the background?",
            "I would like you to change this room background",
        ] {
            assert!(requested(text), "{text}");
        }
        for text in [
            "Hey guys",
            "LOVE IT",
            "How do I change the theme?",
            "Do not change the background",
            "Don't set that theme",
            "Don't use qwave",
            "Please don't change the background",
            "Please change nothing, you shouldn't set a new theme",
            "Why did you change the background?",
            "Can you explain why you changed the theme?",
            "Yesterday I asked you to set the theme",
            "He said 'change the background'",
            "\"Change the background\"",
            "'Change the background'",
            "Aye, she asked you to change the background",
            "Sol, do not change the background",
            "Grok, explain how to set the theme",
            "It would be nice to know if you can change a theme",
            "Show an example of changing a theme",
            "Here is code: change background",
            "{\"tool\":\"set_room_theme\"}",
        ] {
            assert!(!requested(text), "{text}");
        }
    }
    #[test]
    fn presets_and_wire_shapes_are_closed() {
        assert!(action(&obj! {"preset" => "qwave-whisper-v1"}).is_ok());
        for value in [
            Json::Null,
            obj! {"preset" => "https://elsewhere/"},
            obj! {"preset" => "none", "room" => "other"},
            obj! {"preset" => "none", "script" => "alert(1)"},
        ] {
            assert!(action(&value).is_err());
        }
        for value in [
            Json::Null,
            Json::Bool(false),
            Json::from("0"),
            Json::Number("-1".into()),
            Json::Number("1e2".into()),
            Json::Number("1.0".into()),
            Json::from(9_007_199_254_740_000u64),
        ] {
            assert!(revision(&value).is_err());
        }
    }
    #[test]
    fn revisions_cooldown_reset_and_noops_are_bounded() {
        let first = change(&initial(), "qwave-whisper-v1", 0, "Aye", "aye", 100).unwrap();
        assert_eq!(first.get("revision").number(), 1);
        assert!(change(&first, "violet-night-v1", 0, "Other", "other", 200).is_err());
        assert!(change(&first, "violet-night-v1", 1, "Other", "other", 101).is_err());
        assert_eq!(
            change(&first, "qwave-whisper-v1", 1, "Other", "other", 101).unwrap(),
            first
        );
        assert_eq!(
            change(&first, "none", 1, "Other", "other", 101)
                .unwrap()
                .get("revision")
                .number(),
            2
        );
        assert!(change(&first, "violet-night-v1", 1, "Other", "other", 115).is_ok());
    }
}
