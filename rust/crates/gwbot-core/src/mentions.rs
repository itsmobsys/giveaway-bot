//! Splitting a ping into messages that fit Discord's content cap.
//!
//! Used when the per-giveaway entrants role is unavailable and the bot falls
//! back to mentioning members one by one. Pure string logic, kept here so it
//! can be tested without a gateway.

/// Discord's content limit for one message.
pub const CONTENT_LIMIT: usize = 2000;

/// What one mention costs there (`<@1234567890123456789> ` is 22 characters,
/// rounded up for slack).
pub const MENTION_CHARS: usize = 23;

/// Cap on the per-user fallback messages when the entrants role is missing.
/// Five messages cover ~400 mentions; beyond that the command would stall for
/// minutes inside a rate-limited send loop, so it says what it did not cover.
pub const MAX_MENTION_MESSAGES: usize = 5;

/// One message body and how many members it mentions.
pub type MentionChunk = (String, usize);

/// Split mentions into messages that fit Discord's content cap.
///
/// The first chunk carries the announcement text, so its budget is smaller.
/// Sizing chunks on a fixed 80 mentions ignored that: a 256-character prize
/// pushed the first message past 2000, Discord answered 400, and the whole ping
/// was dropped instead of being split. The count is returned alongside the body
/// so callers can report exactly how many members a capped send reached.
pub fn split_mentions(ids: &[String], head: &str) -> Vec<MentionChunk> {
    // `len(head)` is characters in Python; count chars here too, not bytes.
    let head_len = head.chars().count();
    let budget = CONTENT_LIMIT.saturating_sub(head_len).saturating_sub(1);
    let per = (budget / MENTION_CHARS).max(1);
    let mut out = Vec::new();
    let mut start = 0;
    while start < ids.len() {
        let end = (start + per).min(ids.len());
        let block_ids = &ids[start..end];
        let block = block_ids
            .iter()
            .map(|uid| format!("<@{uid}>"))
            .collect::<Vec<_>>()
            .join(" ");
        let body = if start == 0 {
            format!("{head}\n{block}")
        } else {
            block
        };
        out.push((body, block_ids.len()));
        start = end;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(n: usize) -> Vec<String> {
        (0..n).map(|i| format!("1000000000000000{i:03}")).collect()
    }

    #[test]
    fn an_empty_list_produces_no_messages() {
        assert!(split_mentions(&[], "hello").is_empty());
    }

    #[test]
    fn one_short_list_is_one_message_with_the_head() {
        let chunks = split_mentions(&ids(3), "Ping");
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].1, 3);
        assert!(chunks[0].0.starts_with("Ping\n<@"));
    }

    #[test]
    fn a_long_head_shrinks_every_chunk() {
        // The chunk width is sized once, from the first message's budget:
        // 2000 - 199 - 1 = 1800 -> 78 mentions at 23 characters each.
        let chunks = split_mentions(&ids(200), &"x".repeat(199));
        assert_eq!(chunks[0].1, 78);
        // Every later chunk keeps the same width (Python's `range(..., per)`),
        // so no message can drift over the cap.
        assert_eq!(chunks[1].1, 78);
        // A head over budget on its own still yields one mention per message
        // rather than dividing by zero.
        let chunks = split_mentions(&ids(2), &"x".repeat(5000));
        assert_eq!(chunks[0].1, 1);
        assert_eq!(chunks.len(), 2);
    }

    #[test]
    fn every_chunk_stays_under_the_content_limit() {
        // A realistic 256-character prize line, the case that broke the old
        // fixed-80 split.
        let head = "🎉 ".to_string() + &"a".repeat(254);
        let chunks = split_mentions(&ids(500), &head);
        for (body, _) in &chunks {
            assert!(
                body.chars().count() <= CONTENT_LIMIT,
                "chunk was {} chars",
                body.chars().count()
            );
        }
        // The head eats into the shared chunk width, so a long announcement
        // still produces messages under the cap.
        assert!(chunks[0].1 < CONTENT_LIMIT / MENTION_CHARS);
        assert_eq!(chunks[1].1, chunks[0].1);
    }

    #[test]
    fn mention_counts_add_up_to_the_input() {
        let chunks = split_mentions(&ids(1234), "Ping");
        let total: usize = chunks.iter().map(|(_, n)| n).sum();
        assert_eq!(total, 1234);
    }
}
