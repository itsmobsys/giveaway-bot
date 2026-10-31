//! Button definitions and custom-id parsing.
//!
//! The giveaway id is encoded inside each button's `custom_id` (`gw_join:gw_abc`)
//! and every click is dispatched purely off that string. Nothing is registered
//! per giveaway, so a button posted before a restart still works and a
//! long-running process cannot grow a view store without bound.
//!
//! This module owns the wire format only. The Discord adapter turns
//! [`ButtonSpec`] into `serenity` components and calls [`parse_custom_id`] when
//! one is clicked, so the ids are unit-tested without a gateway.

/// Page size of the ephemeral participant list.
pub const PAGE_SIZE: usize = 10;

/// Long enough to match the 15-minute interaction token: after that the buttons
/// are dead anyway.
pub const VIEW_TIMEOUT_SECS: u64 = 900;

pub const JOIN_PREFIX: &str = "gw_join";
pub const LEAVE_PREFIX: &str = "gw_leave";
pub const PARTICIPANTS_PREFIX: &str = "gw_participants";
pub const CLAIM_PREFIX: &str = "gw_claim";
pub const PAGES_PREFIX: &str = "gw_pages";

/// Discord button colours, framework-neutral.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ButtonStyle {
    Primary,
    Secondary,
    Success,
    Link,
}

/// One button to render. A link button carries `url` and no `custom_id`; every
/// other button carries `custom_id` and no `url`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ButtonSpec {
    pub label: String,
    pub emoji: Option<String>,
    pub style: ButtonStyle,
    pub custom_id: Option<String>,
    pub url: Option<String>,
    pub disabled: bool,
}

impl ButtonSpec {
    fn interactive(label: &str, emoji: &str, style: ButtonStyle, custom_id: String) -> Self {
        Self {
            label: label.to_string(),
            emoji: Some(emoji.to_string()),
            style,
            custom_id: Some(custom_id),
            url: None,
            disabled: false,
        }
    }
}

/// True when `raw` is a giveaway id the button format accepts.
///
/// Ids are `gw_` + 12 hex chars, but the pattern is kept loose enough to also
/// match ids written by older revisions, and tight enough to reject anything
/// that is not an id at all.
pub fn valid_id(raw: &str) -> bool {
    !raw.is_empty()
        && raw.len() <= 64
        && raw
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// Build `prefix:id`, or `None` when the id would not round-trip.
pub fn custom_id(prefix: &str, giveaway_id: &str) -> Option<String> {
    valid_id(giveaway_id).then(|| format!("{prefix}:{giveaway_id}"))
}

/// A parsed click, with the giveaway id already extracted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Click {
    Join(String),
    Leave(String),
    Participants(String),
    Claim(String),
    PagePrev,
    PageNext,
    /// The disabled page counter: decoration, never dispatched.
    PageCounter,
}

/// Resolve a raw `custom_id` back to a click, or `None` if it is not ours.
pub fn parse_custom_id(raw: &str) -> Option<Click> {
    if let Some(rest) = raw
        .strip_prefix(PAGES_PREFIX)
        .and_then(|r| r.strip_prefix(':'))
    {
        return match rest {
            "prev" => Some(Click::PagePrev),
            "next" => Some(Click::PageNext),
            "counter" => Some(Click::PageCounter),
            _ => None,
        };
    }
    let (prefix, id) = raw.split_once(':')?;
    if !valid_id(id) {
        return None;
    }
    match prefix {
        JOIN_PREFIX => Some(Click::Join(id.to_string())),
        LEAVE_PREFIX => Some(Click::Leave(id.to_string())),
        PARTICIPANTS_PREFIX => Some(Click::Participants(id.to_string())),
        CLAIM_PREFIX => Some(Click::Claim(id.to_string())),
        _ => None,
    }
}

/// The row of buttons attached to a giveaway message.
///
/// The Dashboard button is a link button: it renders blue and survives restarts
/// without any handler. It is omitted entirely when no URL is configured.
pub fn giveaway_buttons(giveaway_id: &str, dashboard_url: &str) -> Vec<ButtonSpec> {
    let mut buttons = Vec::with_capacity(4);
    if let Some(id) = custom_id(JOIN_PREFIX, giveaway_id) {
        buttons.push(ButtonSpec::interactive(
            "Join",
            "🎟️",
            ButtonStyle::Success,
            id,
        ));
    }
    if let Some(id) = custom_id(LEAVE_PREFIX, giveaway_id) {
        buttons.push(ButtonSpec::interactive(
            "Leave",
            "🚪",
            ButtonStyle::Secondary,
            id,
        ));
    }
    if let Some(id) = custom_id(PARTICIPANTS_PREFIX, giveaway_id) {
        buttons.push(ButtonSpec::interactive(
            "Participants",
            "👥",
            ButtonStyle::Primary,
            id,
        ));
    }
    if !dashboard_url.is_empty() {
        buttons.push(ButtonSpec {
            label: "Dashboard".to_string(),
            emoji: Some("💙".to_string()),
            style: ButtonStyle::Link,
            custom_id: None,
            url: Some(dashboard_url.to_string()),
            disabled: false,
        });
    }
    buttons
}

/// The winner-only Claim Prize button on the ended message.
pub fn claim_buttons(giveaway_id: &str) -> Vec<ButtonSpec> {
    match custom_id(CLAIM_PREFIX, giveaway_id) {
        Some(id) => vec![ButtonSpec::interactive(
            "Claim Prize",
            "🎁",
            ButtonStyle::Primary,
            id,
        )],
        None => Vec::new(),
    }
}

/// `Prev | page x/y | Next`, with the edges disabled.
pub fn participants_pager(page: usize, pages: usize) -> Vec<ButtonSpec> {
    let pages = pages.max(1);
    let page = page.min(pages - 1);
    vec![
        ButtonSpec {
            label: "◀ Previous".to_string(),
            emoji: None,
            style: ButtonStyle::Secondary,
            custom_id: Some(format!("{PAGES_PREFIX}:prev")),
            url: None,
            disabled: page == 0,
        },
        ButtonSpec {
            label: format!("Page {}/{}", page + 1, pages),
            emoji: None,
            style: ButtonStyle::Secondary,
            custom_id: Some(format!("{PAGES_PREFIX}:counter")),
            url: None,
            // No callback on purpose: a disabled component is never dispatched,
            // so the counter is decoration.
            disabled: true,
        },
        ButtonSpec {
            label: "Next ▶".to_string(),
            emoji: None,
            style: ButtonStyle::Secondary,
            custom_id: Some(format!("{PAGES_PREFIX}:next")),
            url: None,
            disabled: page + 1 >= pages,
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn custom_ids_round_trip_for_every_button() {
        for raw in [
            "gw_join:gw_abc",
            "gw_leave:gw_abc",
            "gw_participants:gw_abc",
            "gw_claim:gw_abc",
        ] {
            let parsed = parse_custom_id(raw).expect(raw);
            let id = match parsed {
                Click::Join(id) | Click::Leave(id) | Click::Participants(id) | Click::Claim(id) => {
                    id
                }
                _ => panic!("unexpected page click for {raw}"),
            };
            assert_eq!(id, "gw_abc");
        }
    }

    #[test]
    fn page_ids_parse_without_a_giveaway_id() {
        assert_eq!(parse_custom_id("gw_pages:prev"), Some(Click::PagePrev));
        assert_eq!(parse_custom_id("gw_pages:next"), Some(Click::PageNext));
        assert_eq!(
            parse_custom_id("gw_pages:counter"),
            Some(Click::PageCounter)
        );
        assert_eq!(parse_custom_id("gw_pages:bogus"), None);
    }

    #[test]
    fn foreign_and_malformed_ids_are_rejected() {
        assert_eq!(parse_custom_id("other:gw_abc"), None);
        assert_eq!(parse_custom_id("gw_join"), None);
        assert_eq!(parse_custom_id("gw_join:"), None);
        assert_eq!(parse_custom_id("gw_join:has space"), None);
        assert_eq!(parse_custom_id("gw_join:bad/slash"), None);
        let too_long = "a".repeat(65);
        assert_eq!(parse_custom_id(&format!("gw_join:{too_long}")), None);
    }

    #[test]
    fn giveaway_buttons_match_the_python_labels_and_order() {
        let buttons = giveaway_buttons("gw_abc", "https://example.test/");
        let labels: Vec<&str> = buttons.iter().map(|b| b.label.as_str()).collect();
        assert_eq!(labels, vec!["Join", "Leave", "Participants", "Dashboard"]);
        assert_eq!(buttons[0].style, ButtonStyle::Success);
        assert_eq!(buttons[0].emoji.as_deref(), Some("🎟️"));
        assert_eq!(buttons[3].style, ButtonStyle::Link);
        assert_eq!(buttons[3].url.as_deref(), Some("https://example.test/"));
        assert!(buttons[3].custom_id.is_none());
    }

    #[test]
    fn dashboard_button_is_omitted_without_a_url() {
        let buttons = giveaway_buttons("gw_abc", "");
        assert_eq!(buttons.len(), 3);
        assert!(buttons.iter().all(|b| b.style != ButtonStyle::Link));
    }

    #[test]
    fn an_invalid_id_produces_no_buttons() {
        assert!(giveaway_buttons("bad id", "").is_empty());
        assert!(claim_buttons("bad id").is_empty());
    }

    #[test]
    fn pager_disables_the_edges() {
        let first = participants_pager(0, 3);
        assert!(first[0].disabled);
        assert!(!first[2].disabled);
        assert_eq!(first[1].label, "Page 1/3");
        assert!(first[1].disabled);

        let last = participants_pager(2, 3);
        assert!(!last[0].disabled);
        assert!(last[2].disabled);

        let only = participants_pager(0, 1);
        assert!(only[0].disabled);
        assert!(only[2].disabled);
        assert_eq!(only[1].label, "Page 1/1");
    }
}
