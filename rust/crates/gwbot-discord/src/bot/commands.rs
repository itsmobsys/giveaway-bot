//! Slash commands and autocomplete.
//!
//! `bot.py` registered all of these inside `wire_commands`; here the same table
//! is split in two: this module owns the giveaway lifecycle commands, while
//! [`super::commands_admin`] owns the blacklist/ban/probe set. Both feed the one
//! global command tree registered on the first `ready`.
//!
//! Every handler follows the same shape as its Python original: cheap guards
//! answer immediately (they run before the 3s interaction budget is at risk),
//! then the command is deferred, then the database work happens, and only then
//! is the member answered. The database is never touched before the defer.

use anyhow::Result;
use gwbot_core::components;
use gwbot_core::embeds::{self, Embed};
use gwbot_core::mentions::{split_mentions, MAX_MENTION_MESSAGES};
use gwbot_core::models::ServiceError;
use serenity::all::{
    AutocompleteChoice, ChannelId, CommandDataOptionValue, CommandInteraction, CommandOptionType,
    Context, CreateAllowedMentions, CreateAutocompleteResponse, CreateCommand, CreateCommandOption,
    CreateInteractionResponse, CreateMessage, EditMessage, EditRole, MessageId, RoleId, UserId,
};
use tracing::{debug, error, info, warn};

use super::{cached_kind, guild_id_of, is_text_kind, is_thread_kind, json_id, snowflake, Bot};
use crate::convert;

/// What Discord shows for an option that had no `@app_commands.describe`.
const NO_DESCRIPTION: &str = "No description provided.";

/// The global command table registered on the first `ready`.
pub(crate) fn command_specs() -> Vec<CreateCommand> {
    let mut specs = vec![
        CreateCommand::new("giveaway_create")
            .description("Start a giveaway")
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::String,
                    "prize",
                    "What the winner gets",
                )
                .required(true),
            )
            .add_option(int_opt("winners", "Number of winners (1-25)"))
            .add_option(int_opt("minutes", "How long it runs (minutes)"))
            .add_option(role_opt(
                "required_role_1",
                "Role that can enter, pinged on create (optional)",
            ))
            .add_option(role_opt(
                "required_role_2",
                "Another role that can enter (optional)",
            ))
            .add_option(role_opt(
                "required_role_3",
                "Another role that can enter (optional)",
            ))
            .add_option(role_opt(
                "required_role_4",
                "Another role that can enter (optional)",
            ))
            .add_option(role_opt(
                "required_role_5",
                "Another role that can enter (optional)",
            ))
            .add_option(role_opt(
                "blocked_role",
                "This role cannot enter (optional)",
            ))
            .add_option(int_opt(
                "min_account_age_days",
                "Min Discord account age in days (optional)",
            ))
            .add_option(int_opt(
                "min_messages",
                "Min messages sent in this server (optional)",
            ))
            .add_option(user_opt(
                "host",
                "The hoster shown on the embed — e.g. the prize giver (defaults to you)",
            ))
            .add_option(CreateCommandOption::new(
                CommandOptionType::String,
                "image",
                "Prize photo URL, e.g. a gift-card picture (optional)",
            ))
            .add_option(int_opt(
                "claim_minutes",
                "Winner claim window in minutes, 0 = off (optional)",
            )),
        CreateCommand::new("giveaway_end")
            .description("End a giveaway now and draw")
            .add_option(giveaway_id_opt(true, NO_DESCRIPTION)),
        CreateCommand::new("giveaway_reroll")
            .description("Draw new winner(s)")
            .add_option(giveaway_id_opt(true, "Ended giveaway to reroll"))
            .add_option(int_opt(
                "count",
                "How many new winners to draw (ignored when user is picked)",
            ))
            .add_option(user_opt(
                "user",
                "Replace only this winner (e.g. the one who never claimed)",
            )),
        CreateCommand::new("skipclaim")
            .description("Mark a winner's claim verified (no replacement)")
            .add_option(giveaway_id_opt(true, "Ended giveaway with a claim window"))
            .add_option(
                CreateCommandOption::new(CommandOptionType::User, "user", "The winner to verify")
                    .required(true),
            ),
        CreateCommand::new("giveaway_cancel")
            .description("Cancel an active giveaway")
            .add_option(giveaway_id_opt(true, NO_DESCRIPTION)),
        CreateCommand::new("giveaway_extend")
            .description("Add more time to a running giveaway")
            .add_option(giveaway_id_opt(true, NO_DESCRIPTION))
            .add_option(
                CreateCommandOption::new(
                    CommandOptionType::Integer,
                    "minutes",
                    "Extra minutes to add (1-43200)",
                )
                .required(true),
            ),
        CreateCommand::new("giveaway_list")
            .description("Show entrants, or active giveaways")
            .add_option(giveaway_id_opt(
                false,
                "Leave empty to list active giveaways",
            )),
        CreateCommand::new("giveaway_notifyer")
            .description("One-time setup: role pinged on giveaway news")
            .add_option(role_opt(
                "role",
                "Role pinged on every giveaway (omit to view current)",
            )),
        CreateCommand::new("giveaway_ping")
            .description("Ping everyone who joined a giveaway")
            .add_option(giveaway_id_opt(true, NO_DESCRIPTION))
            .add_option(CreateCommandOption::new(
                CommandOptionType::String,
                "text",
                NO_DESCRIPTION,
            )),
    ];
    specs.extend(super::commands_admin::admin_specs());
    specs
}

/// An integer option with no range: `bot.py` never set an `app_commands.Range`.
fn int_opt(name: &str, description: &str) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::Integer, name, description)
}

fn role_opt(name: &str, description: &str) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::Role, name, description)
}

fn user_opt(name: &str, description: &str) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::User, name, description)
}

/// The `giveaway_id` option, always autocompleted from the tick cache.
fn giveaway_id_opt(required: bool, description: &str) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::String, "giveaway_id", description)
        .required(required)
        .set_autocomplete(true)
}

// -- dispatch ------------------------------------------------------------

pub(crate) async fn handle_command(bot: &Bot, ctx: &Context, ci: CommandInteraction) {
    let name = ci.data.name.clone();
    if super::commands_admin::handle_admin(bot, ctx, &ci, &name).await {
        return;
    }
    let outcome = match name.as_str() {
        "giveaway_create" => giveaway_create(bot, ctx, &ci).await,
        "giveaway_end" => giveaway_end(bot, ctx, &ci).await,
        "giveaway_reroll" => giveaway_reroll(bot, ctx, &ci).await,
        "skipclaim" => skip_claim(bot, ctx, &ci).await,
        "giveaway_cancel" => giveaway_cancel(bot, ctx, &ci).await,
        "giveaway_extend" => giveaway_extend(bot, ctx, &ci).await,
        "giveaway_list" => giveaway_list(bot, ctx, &ci).await,
        "giveaway_notifyer" => giveaway_notifyer(bot, ctx, &ci).await,
        "giveaway_ping" => giveaway_ping(bot, ctx, &ci).await,
        _ => Ok(()),
    };
    if let Err(err) = outcome {
        // `bot.py`'s `on_app_command_error`: the traceback names the command and
        // server, and the member gets a retry hint instead of silence.
        error!("command /{name} failed: {err}");
        bot.safe_followup_command(ctx, &ci, &format!("⚠️ /{name} failed. Please try again."))
            .await;
    }
}

pub(crate) async fn handle_autocomplete(bot: &Bot, ctx: &Context, ci: CommandInteraction) {
    let Some(guild_id) = ci.guild_id else {
        respond_autocomplete(ctx, &ci, Vec::new()).await;
        return;
    };
    // The cache is built on `ready` and on every list refresh; an empty one
    // means this process has not refreshed yet, so load it once here rather
    // than answering with nothing.
    if bot.autocomplete_empty() {
        let live = bot
            .db_call(bot.service().list_all_active(200))
            .await
            .unwrap_or_default();
        bot.cache_autocomplete(&live);
        let ended = bot
            .db_call(bot.service().list_all_ended(200))
            .await
            .unwrap_or_default();
        bot.cache_ended_autocomplete(&ended);
    }
    // The live cache never holds an ended row, so reroll/skipclaim need the
    // twin snapshot or they would suggest nothing usable.
    let ended = matches!(ci.data.name.as_str(), "giveaway_reroll" | "skipclaim");
    let current = ci
        .data
        .autocomplete()
        .map(|option| option.value)
        .unwrap_or_default();
    let choices = bot
        .suggestions(&guild_id.to_string(), current, ended)
        .into_iter()
        .take(25)
        .map(|(id, prize)| {
            let name = truncate_chars(&format!("🏆 {prize} ({id})"), 100);
            AutocompleteChoice::new(name, id)
        })
        .collect();
    respond_autocomplete(ctx, &ci, choices).await;
}

async fn respond_autocomplete(
    ctx: &Context,
    ci: &CommandInteraction,
    choices: Vec<AutocompleteChoice>,
) {
    let response = CreateInteractionResponse::Autocomplete(
        CreateAutocompleteResponse::new().set_choices(choices),
    );
    if let Err(err) = ci.create_response(ctx, response).await {
        debug!("autocomplete response failed: {err}");
    }
}

// -- option helpers ------------------------------------------------------

fn option_value<'a>(ci: &'a CommandInteraction, name: &str) -> Option<&'a CommandDataOptionValue> {
    ci.data
        .options
        .iter()
        .find(|option| option.name == name)
        .map(|option| &option.value)
}

fn string_option(ci: &CommandInteraction, name: &str) -> Option<String> {
    match option_value(ci, name)? {
        CommandDataOptionValue::String(value) => Some(value.clone()),
        _ => None,
    }
}

fn int_option(ci: &CommandInteraction, name: &str) -> Option<i64> {
    match option_value(ci, name)? {
        CommandDataOptionValue::Integer(value) => Some(*value),
        _ => None,
    }
}

fn role_option(ci: &CommandInteraction, name: &str) -> Option<RoleId> {
    match option_value(ci, name)? {
        CommandDataOptionValue::Role(id) => Some(*id),
        _ => None,
    }
}

fn user_option(ci: &CommandInteraction, name: &str) -> Option<UserId> {
    match option_value(ci, name)? {
        CommandDataOptionValue::User(id) => Some(*id),
        _ => None,
    }
}

/// `_can_manage`: the payload's own permission bits, no cache round trip.
fn can_manage(ci: &CommandInteraction) -> bool {
    ci.member
        .as_deref()
        .and_then(|member| member.permissions)
        .map(|permissions| permissions.manage_guild() || permissions.administrator())
        .unwrap_or(false)
}

/// `discord.Member.display_name`: nickname, else global name, else username.
///
/// The interaction payload carries a partial member, so this is the same
/// fallback chain serenity's `Member::display_name` uses rather than a second
/// spelling of it.
fn display_name(ci: &CommandInteraction) -> String {
    ci.member
        .as_deref()
        .and_then(|member| member.nick.clone())
        .or_else(|| ci.user.global_name.clone())
        .unwrap_or_else(|| ci.user.name.clone())
}

/// Python's `s[:n]`, which counts code points rather than bytes.
fn truncate_chars(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    text.chars().take(limit).collect()
}

/// Answer with the service's own refusal text when there is one.
///
/// `ServiceError` is boxed inside `anyhow`, so it has to be recovered before the
/// caller can decide between "tell the member what went wrong" and "this is a
/// real bug, let the generic handler report it".
async fn reply_service_error(
    bot: &Bot,
    ctx: &Context,
    ci: &CommandInteraction,
    err: &anyhow::Error,
) -> bool {
    match err.downcast_ref::<ServiceError>() {
        Some(service) => {
            bot.safe_followup_command(ctx, ci, &format!("⚠️ {}", service.message))
                .await;
            true
        }
        None => false,
    }
}

// -- giveaway_create -----------------------------------------------------

async fn giveaway_create(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    let Some(channel_id) =
        bot.target_channel(&ctx.cache, guild_id, ci.channel.as_ref(), ci.channel_id)
    else {
        // A thread looks like a channel to the person running the command but
        // is not a text channel, and "no text channel available" hid that.
        let problem = if ci
            .channel
            .as_ref()
            .map(|channel| is_thread_kind(channel.kind))
            .unwrap_or(false)
        {
            "Giveaways need a text channel — a thread cannot hold one."
        } else {
            "No text channel available."
        };
        bot.reply_ephemeral(ctx, ci, problem).await;
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }

    let prize = string_option(ci, "prize").unwrap_or_default();
    let winners = int_option(ci, "winners").unwrap_or(1);
    let minutes = int_option(ci, "minutes").unwrap_or(60);
    let required_roles: Vec<String> = [
        "required_role_1",
        "required_role_2",
        "required_role_3",
        "required_role_4",
        "required_role_5",
    ]
    .iter()
    .filter_map(|name| role_option(ci, name))
    .map(|role_id| role_id.to_string())
    .collect();
    let blocked_role = role_option(ci, "blocked_role").map(|role_id| role_id.to_string());
    let min_account_age_days = int_option(ci, "min_account_age_days").unwrap_or(0).max(0);
    let min_messages = int_option(ci, "min_messages").unwrap_or(0).max(0);
    let image = string_option(ci, "image");
    let claim_seconds = int_option(ci, "claim_minutes").unwrap_or(0).max(0) * 60;
    // The host option arrives as a resolved member, so its nickname is already
    // in the payload: no extra fetch to render the embed.
    let (host_id, host_name) = match user_option(ci, "host") {
        Some(user_id) => {
            let nick = ci
                .data
                .resolved
                .members
                .get(&user_id)
                .and_then(|member| member.nick.clone());
            let resolved_user = ci.data.resolved.users.get(&user_id);
            let name = resolved_user
                .and_then(|user| user.global_name.clone())
                .or_else(|| resolved_user.map(|user| user.name.clone()));
            (
                user_id.to_string(),
                nick.or(name).unwrap_or_else(|| user_id.to_string()),
            )
        }
        None => (ci.user.id.to_string(), display_name(ci)),
    };

    let created = bot
        .db_call(bot.service().create(
            &guild_id.to_string(),
            &channel_id.to_string(),
            &prize,
            winners,
            (minutes * 60).max(30),
            &ci.user.id.to_string(),
            None,
            Some(required_roles.as_slice()),
            blocked_role.as_deref(),
            min_account_age_days,
            min_messages,
            image.as_deref(),
            Some(&host_id),
            Some(&host_name),
            claim_seconds,
        ))
        .await;
    let gw = match created {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                error!("create failed: {err}");
                bot.safe_followup_command(ctx, ci, "⚠️ Could not create. Try again.")
                    .await;
            }
            return Ok(());
        }
    };

    // Ping everyone who should know: the server notify role (one-time setup)
    // plus each required role (the people allowed to join).
    let mut pings: Vec<String> = Vec::new();
    let notify = bot.notify_mention(ctx, &guild_id.to_string()).await;
    if !notify.is_empty() {
        pings.push(notify);
    }
    pings.extend(
        required_roles
            .iter()
            .map(|role_id| format!("<@&{role_id}>")),
    );
    let create_content = if pings.is_empty() {
        None
    } else {
        Some(format!("📢 New giveaway! {}", pings.join(" ")))
    };

    let embed = embeds::giveaway_embed(&gw, 0, bot.state.settings.embed_color);
    let rows = convert::to_action_rows(&components::giveaway_buttons(
        &gw.id,
        &bot.state.settings.dashboard_url,
    ));
    let mut message = CreateMessage::new()
        .embed(convert::to_create_embed(&embed))
        .components(rows)
        .allowed_mentions(
            CreateAllowedMentions::new()
                .everyone(false)
                .all_users(false)
                .all_roles(true),
        );
    if let Some(text) = create_content {
        message = message.content(text);
    }
    let msg = match channel_id.send_message(ctx, message).await {
        Ok(msg) => msg,
        Err(err) => {
            // The row already exists. Leaving it would run a giveaway nobody
            // can see and then auto-draw winners into a channel that already
            // refused the bot, so the record is dropped and the reason reported.
            warn!("cannot post giveaway {} in {}: {err}", gw.id, channel_id);
            let _ = bot.db_call(bot.service().discard(&gw.id)).await;
            bot.safe_followup_command(
                ctx,
                ci,
                &format!(
                    "⚠️ I cannot post in <#{channel_id}>. Give me **Send Messages** there \
                     (or set the DISCORD_GIVEAWAY_CHANNEL_ID env var) and try again."
                ),
            )
            .await;
            return Ok(());
        }
    };

    let mut message_note = "";
    if let Err(err) = bot
        .db_call(bot.service().set_message(&gw.id, &msg.id.to_string()))
        .await
    {
        error!("could not save message id for giveaway {}: {err}", gw.id);
        message_note = " (message tracking could not be saved)";
    }

    let mut role_note = "";
    let role_name = format!("🎉 {}", truncate_chars(&prize, 60));
    let body = EditRole::new().name(role_name).mentionable(true);
    match ctx
        .http
        .create_role(
            guild_id,
            &body,
            Some(&format!("Entrants role for giveaway {}", gw.id)),
        )
        .await
    {
        Ok(role) => {
            let saved = bot
                .db_call(
                    bot.service()
                        .set_entrants_role(&gw.id, Some(&role.id.to_string())),
                )
                .await;
            if let Err(err) = saved {
                role_note = " (entrants role could not be saved)";
                error!("could not save entrants role for giveaway {}: {err}", gw.id);
            }
        }
        Err(err) => {
            role_note = " (no entrants role — I need **Manage Roles**)";
            warn!("could not create entrants role in {guild_id}: {err}");
        }
    }

    bot.safe_followup_command(
        ctx,
        ci,
        &format!(
            "✅ Giveaway started: {}{message_note}{role_note}",
            msg.link()
        ),
    )
    .await;
    Ok(())
}

// -- giveaway_end --------------------------------------------------------

async fn giveaway_end(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();

    let resolved = bot
        .db_call(bot.service().resolve(&guild_id.to_string(), &giveaway_id))
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let ended = bot
        .db_call(bot.service().end(&gw.id, None, false, Some(&gw)))
        .await;
    let (ended, winners) = match ended {
        Ok(result) => result,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };

    bot.gw_drop(&gw.id);
    bot.announce(ctx, &ended, &winners, false).await;
    bot.strip_entrants_role(ctx, &ended).await;
    bot.safe_followup_command(ctx, ci, &format!("Ended with {} winner(s).", winners.len()))
        .await;
    Ok(())
}

// -- giveaway_reroll -----------------------------------------------------

async fn giveaway_reroll(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();
    let count = int_option(ci, "count").unwrap_or(1);
    let replace = user_option(ci, "user");

    let resolved = bot
        .db_call(
            bot.service()
                .resolve_ended(&guild_id.to_string(), &giveaway_id),
        )
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let rerolled = match replace {
        Some(user_id) => {
            bot.db_call(bot.service().reroll(&gw.id, 1, Some(&user_id.to_string())))
                .await
        }
        None => {
            bot.db_call(bot.service().reroll(&gw.id, count.max(1), None))
                .await
        }
    };
    let (ended, fresh) = match rerolled {
        Ok(result) => result,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };

    bot.announce(ctx, &ended, &fresh, true).await;
    match (replace, fresh.first()) {
        (Some(user_id), Some(fresh_id)) => {
            bot.safe_followup_command(
                ctx,
                ci,
                &format!("🔁 Replaced <@{user_id}> with <@{fresh_id}>."),
            )
            .await;
        }
        _ => {
            bot.safe_followup_command(ctx, ci, "🔁 Rerolled.").await;
        }
    }
    Ok(())
}

// -- skipclaim -----------------------------------------------------------

async fn skip_claim(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();
    // Required by the spec; a missing value can only mean a malformed payload.
    let Some(user_id) = user_option(ci, "user") else {
        return Ok(());
    };

    let resolved = bot
        .db_call(
            bot.service()
                .resolve_ended(&guild_id.to_string(), &giveaway_id),
        )
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let skipped = bot
        .db_call(
            bot.service()
                .skip_claim(&gw.id, &user_id.to_string(), &ci.user.id.to_string()),
        )
        .await;
    if let Err(err) = skipped {
        if reply_service_error(bot, ctx, ci, &err).await {
            return Ok(());
        }
        error!("skipclaim failed for {giveaway_id}: {err}");
        bot.safe_followup_command(ctx, ci, "⚠️ Could not verify. Try again.")
            .await;
        return Ok(());
    }

    info!("claim skipped for {}/{} by {}", gw.id, user_id, ci.user.id);
    bot.safe_followup_command(
        ctx,
        ci,
        &format!("✅ <@{user_id}>'s claim verified — no replacement will be drawn."),
    )
    .await;
    Ok(())
}

// -- giveaway_cancel -----------------------------------------------------

async fn giveaway_cancel(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();

    let resolved = bot
        .db_call(bot.service().resolve(&guild_id.to_string(), &giveaway_id))
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let cancelled = bot.db_call(bot.service().cancel(&gw.id)).await;
    let ended = match cancelled {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };

    bot.gw_drop(&gw.id);
    // Answer before the cleanup. Stripping entrants one by one can take much
    // longer than the 3 seconds Discord allows for the first response, and the
    // old order made a successful cancel look like a broken command.
    bot.safe_followup_command(ctx, ci, &format!("🚫 Cancelled **{}**.", ended.prize))
        .await;

    let channel_id = snowflake(&ended.channel_id).map(ChannelId::new);
    let is_text = channel_id
        .map(|channel_id| {
            cached_kind(&ctx.cache, guild_id_of(&ended), channel_id)
                .map(is_text_kind)
                .unwrap_or(false)
        })
        .unwrap_or(false);

    if is_text {
        if let (Some(channel_id), Some(message_id)) = (
            channel_id,
            ended
                .message_id
                .as_deref()
                .and_then(snowflake)
                .map(MessageId::new),
        ) {
            let cancelled_embed = Embed {
                title: "Giveaway Cancelled".to_string(),
                description: format!("🚫 **{}** — this giveaway was cancelled.", ended.prize),
                colour: bot.state.settings.embed_color,
                image_url: None,
                footer: Some(format!("ID: {}", ended.id)),
            };
            let edited = channel_id
                .edit_message(
                    ctx,
                    message_id,
                    EditMessage::new()
                        .embed(convert::to_create_embed(&cancelled_embed))
                        .components(Vec::new()),
                )
                .await;
            if let Err(err) = edited {
                warn!(
                    "could not close cancelled giveaway message {}: {err}",
                    ended.id
                );
            }
        }
    }

    bot.strip_entrants_role(ctx, &ended).await;

    if is_text {
        if let Some(channel_id) = channel_id {
            let notify = bot.notify_mention(ctx, &guild_id.to_string()).await;
            let mut text = format!("🚫 Giveaway **{}** was cancelled.", ended.prize);
            if !notify.is_empty() {
                text.push(' ');
                text.push_str(&notify);
            }
            let _ = channel_id
                .send_message(
                    ctx,
                    CreateMessage::new().content(text).allowed_mentions(
                        CreateAllowedMentions::new()
                            .everyone(false)
                            .all_users(false)
                            .all_roles(true),
                    ),
                )
                .await;
        }
    }
    Ok(())
}

// -- giveaway_extend -----------------------------------------------------

async fn giveaway_extend(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();
    let minutes = int_option(ci, "minutes").unwrap_or(0);

    let resolved = bot
        .db_call(bot.service().resolve(&guild_id.to_string(), &giveaway_id))
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let extended = bot
        .db_call(bot.service().extend(&gw.id, minutes.max(1) * 60))
        .await;
    let fresh = match extended {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };

    bot.gw_drop(&gw.id);
    bot.refresh_embed(ctx, &fresh, None, None).await;
    let ends_in = fresh.ends_at / 1000;
    bot.safe_followup_command(
        ctx,
        ci,
        &format!(
            "⏳ **{}** extended — now ends <t:{ends_in}:R>.",
            fresh.prize
        ),
    )
    .await;

    // Tell the people waiting: ping every entrant in the giveaway channel.
    let entrants = match bot.db_call(bot.service().entries(&fresh.id, None)).await {
        Ok(rows) => rows,
        Err(err) => {
            error!(
                "entrant lookup failed for extend notice ({}): {err}",
                fresh.id
            );
            Vec::new()
        }
    };
    if entrants.is_empty() {
        return Ok(());
    }
    let Some(channel_id) = snowflake(&fresh.channel_id).map(ChannelId::new) else {
        return Ok(());
    };
    let is_text = cached_kind(&ctx.cache, guild_id_of(&fresh), channel_id)
        .map(is_text_kind)
        .unwrap_or(false);
    if !is_text {
        return Ok(());
    }
    let notice = format!(
        "⏳ **{}** got more time — now ends <t:{ends_in}:R>!",
        fresh.prize
    );
    match bot.role_for(ctx, &fresh) {
        Some((_, role_id)) => {
            let sent = channel_id
                .send_message(
                    ctx,
                    CreateMessage::new()
                        .content(format!("{notice}\n<@&{role_id}>"))
                        .allowed_mentions(
                            CreateAllowedMentions::new()
                                .everyone(false)
                                .all_users(false)
                                .all_roles(true),
                        ),
                )
                .await;
            if let Err(err) = sent {
                warn!("extend notice failed for {} ({err})", fresh.id);
            }
        }
        None => {
            let ids: Vec<String> = entrants.iter().filter_map(json_id).collect();
            let pairs = split_mentions(&ids, &notice);
            let capped = &pairs[..pairs.len().min(MAX_MENTION_MESSAGES)];
            for (chunk, _) in capped {
                let sent = channel_id
                    .send_message(
                        ctx,
                        CreateMessage::new().content(chunk).allowed_mentions(
                            CreateAllowedMentions::new()
                                .everyone(false)
                                .all_users(true)
                                .all_roles(false),
                        ),
                    )
                    .await;
                if let Err(err) = sent {
                    warn!("extend notice failed for {} ({err})", fresh.id);
                    return Ok(());
                }
            }
            if pairs.len() > capped.len() {
                let reached: usize = capped.iter().map(|(_, count)| *count).sum();
                bot.safe_followup_command(
                    ctx,
                    ci,
                    &format!(
                        "⏳ Extended, but only {reached} of {} entrants could be pinged — \
                         recreate the entrants role (delete it and re-run the command) to \
                         ping the rest.",
                        ids.len()
                    ),
                )
                .await;
            }
        }
    }
    Ok(())
}

// -- giveaway_list -------------------------------------------------------

async fn giveaway_list(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "Use this in a server.").await;
        return Ok(());
    };
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let gid = guild_id.to_string();
    let giveaway_id = string_option(ci, "giveaway_id").filter(|raw| !raw.is_empty());

    if let Some(giveaway_id) = giveaway_id {
        let resolved = bot.db_call(bot.service().resolve(&gid, &giveaway_id)).await;
        let gw = match resolved {
            Ok(gw) => gw,
            Err(err) => {
                if !reply_service_error(bot, ctx, ci, &err).await {
                    return Err(err);
                }
                return Ok(());
            }
        };
        // Count and page separately: the total is what the header needs, and
        // only 50 of them are ever printed.
        let total = bot
            .db_call(bot.service().entry_count(&gw.id, false))
            .await?;
        let entrants = bot.db_call(bot.service().entries(&gw.id, Some(50))).await?;
        let host = match gw.host_id.as_deref().filter(|id| !id.is_empty()) {
            Some(host_id) => format!(" by <@{host_id}>"),
            None => String::new(),
        };
        let chance = if total != 0 {
            (gw.winner_count as f64 / total as f64 * 100.0).min(100.0)
        } else {
            0.0
        };
        let odds = format!(
            "\n📊 Each entrant has a **{chance:.1}%** chance ({} winner(s) / {total} entries).",
            gw.winner_count
        );
        if entrants.is_empty() {
            bot.safe_followup_command(
                ctx,
                ci,
                &format!("🏆 **{}**{host} — no entrants yet.", gw.prize),
            )
            .await;
            return Ok(());
        }
        let shown: Vec<String> = entrants
            .iter()
            .filter_map(json_id)
            .map(|user_id| format!("<@{user_id}>"))
            .collect();
        let extra = if total > entrants.len() as i64 {
            format!("\n…and {} more.", total - entrants.len() as i64)
        } else {
            String::new()
        };
        let status = if gw.active() {
            "running 🟢".to_string()
        } else {
            gw.status.clone()
        };
        bot.safe_followup_command(
            ctx,
            ci,
            &format!(
                "🏆 **{}**{host} — **{total}** entrant(s) ({status}){odds}:\n{}{extra}",
                gw.prize,
                shown.join(", ")
            ),
        )
        .await;
        return Ok(());
    }

    let active = bot.db_call(bot.service().list_active(&gid)).await?;
    if active.is_empty() {
        bot.safe_followup_command(ctx, ci, "No active giveaways.")
            .await;
        return Ok(());
    }
    let mut lines: Vec<String> = Vec::new();
    for gw in active.iter().take(10) {
        let count = bot
            .db_call(bot.service().entry_count(&gw.id, false))
            .await?;
        let each = if count != 0 {
            format!(
                " — **{:.1}%** each",
                (gw.winner_count as f64 / count as f64 * 100.0).min(100.0)
            )
        } else {
            String::new()
        };
        // Ten lines carrying full 256-character prizes pass Discord's 2000
        // character cap, and the resulting 400 would be swallowed by
        // `safe_followup_command` — the moderator would get no reply at all.
        let prize = if gw.prize.chars().count() <= 80 {
            gw.prize.clone()
        } else {
            format!("{}…", truncate_chars(&gw.prize, 77))
        };
        lines.push(format!(
            "• **{prize}** — {count} entries{each} — `{}` — <t:{}:R>",
            gw.id,
            gw.ends_at / 1000
        ));
    }
    lines.push("\nTip: run `/giveaway_list` with an id to see who entered.".to_string());
    bot.safe_followup_command(ctx, ci, &lines.join("\n")).await;
    Ok(())
}

// -- giveaway_notifyer ---------------------------------------------------

async fn giveaway_notifyer(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    let gid = guild_id.to_string();
    // Ack first: this command does an HTTP role edit plus a database write
    // before it can answer, and Discord only allows 3 seconds.
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }

    let Some(role_id) = role_option(ci, "role") else {
        let current = bot
            .db_call(bot.service().get_notify_role(&gid))
            .await
            .ok()
            .flatten();
        // A hand-edited non-numeric id must read as "unset", not crash the
        // command, and a role deleted since it was saved is equally useless.
        let existing = current
            .as_deref()
            .filter(|raw| !raw.is_empty() && raw.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|raw| raw.parse::<u64>().ok())
            .map(RoleId::new)
            .filter(|role_id| {
                ctx.cache
                    .guild(guild_id)
                    .map(|guild| guild.roles.contains_key(role_id))
                    .unwrap_or(false)
            });
        match existing {
            Some(role_id) => {
                bot.safe_followup_command(
                    ctx,
                    ci,
                    &format!(
                        "🔔 Notify role: <@&{role_id}> — give it to members so they get \
                         pinged on giveaway news."
                    ),
                )
                .await;
            }
            None => {
                bot.safe_followup_command(
                    ctx,
                    ci,
                    "No notify role set. Run `/giveaway_notifyer role:@YourRole` once.",
                )
                .await;
            }
        }
        return Ok(());
    };

    let mut note = "";
    let body = EditRole::new().mentionable(true);
    let edited = ctx
        .http
        .edit_role(guild_id, role_id, &body, Some("Giveaway notify role setup"))
        .await;
    if let Err(err) = edited {
        // A role that cannot be made mentionable pings nobody. Saving it anyway
        // is fine, but claiming success is not.
        warn!("could not make notify role {role_id} mentionable: {err}");
        note = " — but I could not make it mentionable (I need **Manage Roles**)";
    }
    if let Err(err) = bot
        .db_call(
            bot.service()
                .set_notify_role(&gid, Some(&role_id.to_string())),
        )
        .await
    {
        error!("notify role save failed: {err}");
        bot.safe_followup_command(ctx, ci, "⚠️ Could not save. Try again.")
            .await;
        return Ok(());
    }
    // Invalidate the cached notify role now, not in 10s: the mention appended to
    // the very next announcement must be the role just set.
    bot.service().invalidate_notify_role(&gid).await;
    bot.safe_followup_command(
        ctx,
        ci,
        &format!(
            "✅ <@&{role_id}> will now be pinged on every giveaway (new, winners, \
             rerolls, cancellations){note}."
        ),
    )
    .await;
    Ok(())
}

// -- giveaway_ping -------------------------------------------------------

async fn giveaway_ping(bot: &Bot, ctx: &Context, ci: &CommandInteraction) -> Result<()> {
    let Some(guild_id) = ci.guild_id else {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    };
    if !can_manage(ci) {
        bot.reply_ephemeral(ctx, ci, "You need **Manage Server**.")
            .await;
        return Ok(());
    }
    if !bot.defer_command(ctx, ci).await {
        return Ok(());
    }
    let giveaway_id = string_option(ci, "giveaway_id").unwrap_or_default();

    let resolved = bot
        .db_call(bot.service().resolve(&guild_id.to_string(), &giveaway_id))
        .await;
    let gw = match resolved {
        Ok(gw) => gw,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    let entrants = bot.db_call(bot.service().entries(&gw.id, None)).await;
    let entrants = match entrants {
        Ok(rows) => rows,
        Err(err) => {
            if !reply_service_error(bot, ctx, ci, &err).await {
                return Err(err);
            }
            return Ok(());
        }
    };
    if entrants.is_empty() {
        bot.safe_followup_command(ctx, ci, "Nobody has joined this giveaway yet.")
            .await;
        return Ok(());
    }

    // Discord accepts up to 6000 characters on an option but only allows 2000
    // in a message, so a long announcement has to be cut here rather than
    // turning into a 400 that reads like a permissions problem.
    let text = string_option(ci, "text")
        .unwrap_or_default()
        .trim()
        .to_string();
    let text = if text.chars().count() > 1400 {
        format!("{}…", truncate_chars(&text, 1397))
    } else {
        text
    };
    let body = if text.is_empty() {
        format!("📢 **{}**", gw.prize)
    } else {
        format!("📢 **{}** — {text}", gw.prize)
    };

    let role = bot.role_for(ctx, &gw);
    let channel_id = ci.channel_id;
    let is_text = bot
        .channel_kind(&ctx.cache, guild_id, ci.channel.as_ref(), channel_id)
        .map(is_text_kind)
        .unwrap_or(false);
    if !is_text {
        bot.safe_followup_command(ctx, ci, "Run this in a text channel.")
            .await;
        return Ok(());
    }

    let mut pairs: Vec<(String, usize)> = Vec::new();
    let mut sent: usize = 0;
    match role {
        Some((_, role_id)) => {
            let sent_message = channel_id
                .send_message(
                    ctx,
                    CreateMessage::new()
                        .content(format!("{body}\n<@&{role_id}>"))
                        .allowed_mentions(
                            CreateAllowedMentions::new()
                                .everyone(false)
                                .all_users(false)
                                .all_roles(true),
                        ),
                )
                .await;
            if let Err(err) = sent_message {
                warn!("ping failed for {} ({err})", gw.id);
                bot.safe_followup_command(ctx, ci, "⚠️ I cannot send messages there.")
                    .await;
                return Ok(());
            }
        }
        None => {
            let ids: Vec<String> = entrants.iter().filter_map(json_id).collect();
            pairs = split_mentions(&ids, &body);
            for (chunk, count) in pairs.iter().take(MAX_MENTION_MESSAGES) {
                let sent_message = channel_id
                    .send_message(
                        ctx,
                        CreateMessage::new().content(chunk).allowed_mentions(
                            CreateAllowedMentions::new()
                                .everyone(false)
                                .all_users(true)
                                .all_roles(false),
                        ),
                    )
                    .await;
                if let Err(err) = sent_message {
                    warn!("ping failed for {} ({err})", gw.id);
                    bot.safe_followup_command(ctx, ci, "⚠️ I cannot send messages there.")
                        .await;
                    return Ok(());
                }
                sent += count;
            }
        }
    }

    if role.is_none() && pairs.len() > MAX_MENTION_MESSAGES {
        bot.safe_followup_command(
            ctx,
            ci,
            &format!(
                "📢 Pinged {sent} of {} entrant(s) — capped at {MAX_MENTION_MESSAGES} messages \
                 without an entrants role. Re-run once the role exists to reach the rest.",
                entrants.len()
            ),
        )
        .await;
    } else {
        bot.safe_followup_command(
            ctx,
            ci,
            &format!("📢 Pinged {} entrant(s).", entrants.len()),
        )
        .await;
    }
    Ok(())
}

// -- shared cleanup ------------------------------------------------------
//
// The entrants-role strip lives on `Bot` (`bot::tick`), because the tick loop
// needs the same body without an event context to borrow.
