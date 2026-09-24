# Telegram group → channel membership bot

This bot watches a private employee group. When it sees a group member who is not in the private channel, it posts a short invitation in the group. Each invitation link requests approval; the bot approves only the named person's Telegram account and then revokes that link. Other people who click it are declined. Links expire after 24 hours, and reminders are limited to once per person per 24 hours.

The bot checks on group joins, observed group messages, channel departures, and a six-hour scan of members it has observed. Telegram's Bot API does **not** provide a full group member list, so members who were present before the bot started will be checked when they next send a message or have a membership change. The bot cannot guarantee a complete audit of silent, preexisting group members. It also does not invite channel-only subscribers into the group.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token private.
2. Add it as an administrator in both the employee group and channel. In the channel, grant **Invite Users**. In the group, it needs to receive member updates and send messages. Admin status lets it observe group messages with privacy mode on.
3. Find both chat IDs. With the bot added, run `BOT_TOKEN='your-token' python3 membership_bot.py --discover`, then send a message in the group and publish a post in the channel. The command prints their IDs. Stop it with Ctrl+C.
4. Start the bot with persistent storage:

   ```sh
   export BOT_TOKEN='your-token'
   export GROUP_ID='-1001234567890'
   export CHANNEL_ID='-1009876543210'
   export BOT_DB='/persistent/path/membership.sqlite3'
   python3 membership_bot.py
   ```

Run one instance of this bot continuously. The SQLite file stores observed group members, invitation links, and the update offset. Keep that file private and backed up. If the bot previously had a webhook, remove it before using long polling. The process logs API errors and retries; check logs if it cannot post or approve requests.

## Behavior and limits

- The approval step makes the link usable only by the intended Telegram account, even though it is visible in the group. Telegram does not allow `member_limit=1` on approval-based links; the bot revokes the link after one approved join.
- Other channel administrators should leave requests from these links to the bot; they can manually approve someone the bot would decline.
- A request made through another administrator's channel link is left for that administrator. The bot handles only links it created.
- Telegram can retain updates for at most 24 hours. Keep the bot running so it sees membership changes.
- This bot needs a private channel that supports invite links and join requests. It does not remove members from either chat.

## Test

```sh
python3 -m unittest -v
```
