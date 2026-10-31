//! Converting the framework-neutral specs in `gwbot-core` into serenity types.
//!
//! Keeping this in one file means the exact wording, colours and button order
//! live in `gwbot-core` (and are unit-tested there), while this crate only has
//! to know how serenity spells the same thing.

use gwbot_core::components::{ButtonSpec, ButtonStyle};
use gwbot_core::embeds::Embed;
use serenity::all::{
    ButtonStyle as SButtonStyle, Colour, CreateActionRow, CreateButton, CreateEmbed,
};
use serenity::model::channel::ReactionType;

/// Turn a rendered embed into a serenity embed builder.
pub fn to_create_embed(embed: &Embed) -> CreateEmbed {
    let mut out = CreateEmbed::new()
        .title(&embed.title)
        .description(&embed.description)
        .colour(Colour::from(embed.colour));
    if let Some(image) = &embed.image_url {
        // Only ever an http(s) URL; Discord rejects anything else with a 400,
        // and the service already refuses to store non-URLs.
        out = out.image(image);
    }
    if let Some(footer) = &embed.footer {
        out = out.footer(serenity::builder::CreateEmbedFooter::new(footer));
    }
    out
}

fn to_style(style: ButtonStyle) -> SButtonStyle {
    match style {
        ButtonStyle::Primary => SButtonStyle::Primary,
        ButtonStyle::Secondary => SButtonStyle::Secondary,
        ButtonStyle::Success => SButtonStyle::Success,
        // serenity has no Link variant: a link button is identified by its URL
        // and `CreateButton::new_link` sets the style itself. Callers route
        // Link through that path before they get here, so this arm only exists
        // to keep the mapping total.
        ButtonStyle::Link => SButtonStyle::Primary,
    }
}

/// Turn one button spec into a serenity button.
///
/// A link button is built with `new_link` and carries no custom id; everything
/// else is built with `new` and must carry one, so a malformed spec degrades to
/// a disabled button instead of panicking mid-response.
pub fn to_create_button(spec: &ButtonSpec) -> CreateButton {
    let style = to_style(spec.style);
    let mut button = match (spec.style, &spec.url) {
        (ButtonStyle::Link, Some(url)) => CreateButton::new_link(url),
        (ButtonStyle::Link, None) => {
            // A link button with no URL cannot be rendered at all. Keep the
            // shape valid: a disabled primary with a placeholder id.
            let mut b = CreateButton::new("gw_invalid_link").label(&spec.label);
            b = b.style(SButtonStyle::Secondary).disabled(true);
            return b;
        }
        (_, _) => match &spec.custom_id {
            Some(id) => CreateButton::new(id),
            None => {
                let b = CreateButton::new("gw_invalid").label(&spec.label);
                return b.style(SButtonStyle::Secondary).disabled(true);
            }
        },
    };
    button = button.style(style).label(&spec.label);
    if let Some(emoji) = &spec.emoji {
        button = button.emoji(ReactionType::Unicode(emoji.clone()));
    }
    if spec.disabled {
        button = button.disabled(true);
    }
    button
}

/// One row of buttons. Discord caps a row at five components.
pub fn to_action_row(specs: &[ButtonSpec]) -> CreateActionRow {
    CreateActionRow::Buttons(specs.iter().take(5).map(to_create_button).collect())
}

/// Rows for a spec list, chunked five at a time (Discord's hard limit).
pub fn to_action_rows(specs: &[ButtonSpec]) -> Vec<CreateActionRow> {
    specs.chunks(5).map(to_action_row).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use gwbot_core::components::{giveaway_buttons, participants_pager};

    #[test]
    fn every_giveaway_button_converts_without_panicking() {
        let specs = giveaway_buttons("gw_abc", "https://example.test/");
        let row = to_action_row(&specs);
        match row {
            CreateActionRow::Buttons(buttons) => assert_eq!(buttons.len(), 4),
            _ => panic!("expected a button row"),
        }
    }

    #[test]
    fn a_dashboard_link_button_needs_no_custom_id() {
        let specs = giveaway_buttons("gw_abc", "https://example.test/");
        let link = specs.last().expect("dashboard button");
        assert_eq!(link.style, ButtonStyle::Link);
        // Building it must not fall into the "invalid link" branch, which is
        // what a missing URL produces.
        let _ = to_create_button(link);
        let broken = ButtonSpec {
            label: "Dashboard".into(),
            emoji: None,
            style: ButtonStyle::Link,
            custom_id: None,
            url: None,
            disabled: false,
        };
        let _ = to_create_button(&broken);
    }

    #[test]
    fn rows_never_exceed_discords_five_component_cap() {
        let mut specs = giveaway_buttons("gw_abc", "https://example.test/");
        specs.extend(participants_pager(0, 2));
        let rows = to_action_rows(&specs);
        assert_eq!(rows.len(), 2);
        for row in rows {
            match row {
                CreateActionRow::Buttons(buttons) => assert!(buttons.len() <= 5),
                _ => panic!("expected button rows"),
            }
        }
    }
}
