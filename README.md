# da-sales-watcher

A small Docker container that checks your DeviantArt earnings every few minutes and sends a push notification (ntfy and/or Telegram) when the number goes up. DeviantArt has no sales API, so it reads the earnings page with your logged-in browser cookie. Because it watches the total, it catches every kind of sale: subscriptions, Premium Galleries, paid downloads, Adoptables.

It also tells you when the login expires, when DeviantArt changes the page so the number can't be read, and when DeviantArt shows a bot check instead of the page.

## 1. Notifications

**ntfy (easiest)**
1. Install **ntfy** on your phone (Play Store, F-Droid or App Store).
2. Tap **+** and subscribe to a topic with a long random name, like `da-sales-k39xq7pz`. Anyone who knows the name can read it, so make it hard to guess.
3. On the PC, open `https://ntfy.sh/<your-topic>` in the browser and allow notifications.
4. Put `https://ntfy.sh/<your-topic>` in `NTFY_URL`.

**Telegram (optional, instead of or as well as ntfy)**
1. Talk to **@BotFather**, send `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to the new bot, open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` number into `TELEGRAM_CHAT_ID`.

## 2. The cookie

Best in a separate browser profile used only for this, so logging out in your normal browser never ends the watcher's session.

1. Log in to DeviantArt and open `https://www.deviantart.com/studio/earnings`.
2. Press **F12**, open **Network** and reload.
3. Right-click the `earnings` request, **Copy Value → Copy as cURL**, and paste it into a text editor.
4. Copy what comes after `Cookie: ` up to the closing quote into `DA_COOKIE`. It must contain `auth=` and `auth_secure=`, and no `…` (some header panels cut long values with one).
5. Copy the `User-Agent` value into `DA_USER_AGENT`.
6. Close the window **without logging out**. Logging out, changing the password or "log out of all devices" ends the session.

Your cookie is your login. Keep `.env` private and never share it.

## 3. Install and test

```bash
cd da-sales-watcher
cp .env.example .env
nano .env                      # fill in steps 1 and 2

docker compose build
docker compose run --rm da-sales-watcher python watcher.py --test
```

You should see `✓ Found value: $...` and get a test notification. If the regex finds nothing, the test prints the start of the page so you can adjust `VALUE_REGEX`. Every group in parentheses is added up; the default adds the available balance and the pending amount.

## 4. Run it

```bash
docker compose up -d
docker compose logs -f         # Ctrl+C to leave
```

| Alert | When |
|---|---|
| DA watcher started | First run, no saved value yet |
| New sale on DeviantArt! | The total went up: `+$20.00 (total now $612.40)` |
| DA earnings went down | A withdrawal or refund (low priority) |
| DA watcher: login expired | DeviantArt sent it to the login page. Repeats every `LOGIN_REMIND_HOURS` (6) |
| DA watcher back online | First good read after a login expired alert |
| DA watcher: can't read earnings | The page loaded but the number wasn't found: adjust `VALUE_REGEX` |
| DA watcher: DeviantArt bot check | DeviantArt answered with a browser check instead of the page |

Network errors and unexpected errors are only logged, not sent. A quiet phone doesn't prove the watcher is healthy, so check `docker compose logs` now and then: a good check logs `No change` or `SALE`.

## How the login stays alive

DeviantArt rotates its login cookie. The watcher saves every refreshed cookie to `data/cookies.json` and uses it from then on, like a browser does. That file needs the `./data:/data` volume so it survives restarts. Treat it like a password.

When you paste a new cookie into `.env`, the watcher notices the change and starts from the new one:

```bash
docker compose up -d --force-recreate
```

## The bot check

Since November 2026 DeviantArt can answer `/studio/earnings` with an AWS WAF JavaScript challenge (HTTP 202, empty body, `x-amzn-waf-action: challenge`). The rule is case-sensitive, so the watcher then tries `/STUDIO/earnings`, which serves the same page. If both are blocked you get the bot check alert, and the fetch needs a real browser (for example headless Playwright).

## Notes

- Keep the interval at 5 minutes or more. It's one light request per check, like having the page open in a tab.
- Several sales between two checks arrive as one alert with the combined amount.
- Not affiliated with DeviantArt. Reading your own earnings page with your own cookie is the same as opening it in a browser; use it at your own risk.
