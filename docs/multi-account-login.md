# Running several AI subscription accounts side by side

This widget can track quota for more than one AI coding subscription at a
time, including **two accounts with the same vendor** on two different email
addresses. That second case is the one that goes wrong, and it goes wrong for
a reason that has nothing to do with this widget. Read section 1 before you
try anything.

Everything below describes behaviour that exists today. Where something is
unverified or unsupported, it says so.

---

## 1. Why a second account is hard

There are two separate obstacles. People usually find the first one, fix it,
and stay stuck on the second.

### Half one: the CLI stores exactly one login

`claude` and `codex` each keep their credential in a single, fixed,
per-user file:

| Vendor CLI | Default credential file |
| --- | --- |
| Claude Code | `<home>/.claude/.credentials.json` |
| OpenAI Codex | `<home>/.codex/auth.json` |

One file, one login. Signing in as a second account with the vendor's own CLI
does not give you two logins; it overwrites the one you had. Running the login
command again usually does not even offer you a sign-in - it finds the
existing session and reuses it.

This widget works around that by keeping its **own** vault: it copies each
captured credential into its own store, so a login survives the next login and
the CLI's one-slot limit stops mattering.

To make a genuinely fresh sign-in possible at all, the widget runs the vendor
CLI inside a **disposable sandbox directory that acts as a fake home**. The
CLI looks for an existing session, finds nothing, and has to prompt. When the
sign-in finishes the credential is copied into the vault, and the sandbox -
which held a real credential in a plain file while the login was running - is
deleted. Teardown happens on success, failure, timeout, cancellation and the
unexpected error nobody predicted, alike.

### Half two - the one people skip: your browser is still signed in

**This is the most likely single reason you cannot get the second account in.**

These logins are an OAuth round-trip through a real browser. The sandbox
isolates the *CLI*. It does not, and cannot, isolate your *browser*.

So the sequence goes: the sandboxed CLI opens a sign-in URL, your browser is
still signed in to account #1, the vendor's site sees a valid session, shows
no account chooser and approves immediately, and the CLI receives a credential
for **account #1 again**. Nothing errored. Nothing warned you. You just got the
same account back, and it looks exactly like the software is broken.

Both halves have to be broken for a second account to get in:

1. the CLI must not find an existing login - the sandbox handles that;
2. the **browser** must not have a live vendor session - **you** have to handle
   that, by signing out on the vendor's website or using a private/incognito
   window.

The widget does detect this after the fact. When a sign-in returns an account
that is already in the vault it refuses to add a second copy, and says:

> This is the same account you are already signed in as. Sign out in your
> browser, or open a private/incognito window, before signing in with the
> other email address.

That message is not a generic failure. It means the sign-in worked perfectly
and authenticated the wrong account.

---

## 2. Adding a second account of the same vendor

Do these in order. Step 2 is not optional.

1. **Make sure quota lookups and logins can run at all.** If you are in a
   region the vendors block, the widget refuses to send anything until a VPN
   is up - see section 8. A refusal here is intentional, and no amount of
   retrying will get past it.

2. **Sign your browser out of the vendor, or prepare a private window.** Open
   the vendor's website in your default browser and sign out of account #1 -
   or plan to complete the sign-in in a private/incognito window with no
   session in it. Skipping this step is what makes the whole procedure hand
   you account #1 again.

3. **Open the widget's AI accounts pane and press `+`.** A provider picker
   opens.

4. **Pick the vendor.** Only Claude Code and OpenAI Codex can actually report
   usage - see section 6. The widget creates a disposable sandbox and opens a
   console window running the vendor's own login command inside it.

5. **Complete the sign-in in that console, and in the browser it opens, as the
   second email address.** If the browser shows no account chooser and no
   credential prompt, stop: you are about to re-authenticate account #1.
   Cancel, go back to step 2, and use a private window this time.

   The widget waits a few minutes for you. If you walk away, cancel the
   sign-in rather than killing the widget - cancelling tears the sandbox down;
   killing the widget can leave it, and the credential inside it, on disk.

6. **Read the widget's reply.**
   - *account added* - the credential was captured and vaulted.
   - The duplicate message from section 1 - same account again. Nothing was
     added. Go to section 3.
   - Anything else - the sign-in did not complete, and the message names the
     reason.

7. **Give it a label you will recognise.** The widget deliberately never reads
   an email address out of a credential, so the default label just names the
   provider. Rename it yourself - in the pane, or with
   `aiacct rename <ACCOUNT_ID> "<NEW_LABEL>"`.

### Verifying it is genuinely a different account

Do not assume. Check:

1. Run `aiacct list`. You should now see **two rows** for that provider, with
   **two different ids**. One row means nothing was added.

2. Compare the `EXPIRES` column of the two rows. Two independent sign-ins get
   independent expiry times; identical expiry on both rows is a reason to look
   harder.

3. The strongest check available: run `aiacct usage <ACCOUNT_ID>` for each of
   the two ids and confirm the reported figures are not identical. One account
   heavily used and one barely used makes this obvious. This needs network
   access, and a VPN in a gated region.

Be clear about what the widget can and cannot prove here. It tells accounts
apart by a **fingerprint built from non-secret credential fields - never from
the token**. For Codex that fingerprint is an account identifier, which is a
real identity check. For Claude it is derived from subscription type and
expiry, which reliably collapses two copies of the *same* login into one
entry, but is not an assertion about which email address signed in. If the
credential shape is not one the widget recognises, it says it cannot tell
whether the account is new rather than guessing.

---

## 3. When the same account comes back anyway

Work down this list. Each step has something specific to observe.

1. **Confirm that is actually what happened.** Read the widget's notice. If it
   is the "same account you are already signed in as" message, the sign-in
   succeeded as the wrong account - continue down this list. If it is a
   timeout, a cancellation, or "isolated login not available", that is a
   different problem and this checklist is not for it.

2. **Sign out on the vendor's website in the browser the login actually
   opens** - your default browser, not whichever one you had in mind. Reload
   the vendor's site afterwards. Expected observation: a signed-out landing
   page or a login form. If it still shows you an account, you signed out
   somewhere else.

3. **Retry the add in a private/incognito window instead.** Expected
   observation during step 5 of the recipe: the vendor asks for an email
   address and a password or a code. If it does not ask, the window had a
   session in it after all.

4. **Look for a "continue as ..." shortcut or a second browser profile.** Many
   vendor pages offer one-click continue for a remembered account. Expected
   observation: a real credential prompt, not a continue-as button.

5. **Check what is actually registered.** Run `aiacct list`, then
   `aiacct show <ACCOUNT_ID>` on each row. Expected observation: exactly as
   many rows as accounts you believe you have. A row you thought you removed
   is still a row the duplicate detector compares against.

6. **Run `aiacct doctor`.** Expected observation: a problem count of zero, and
   no credential files reported on disk that are not in the vault. If it
   reports unregistered credentials, or the same login in more than one
   location, see section 5 - that distorts what you are seeing.

7. **If `doctor` reports a stale registered copy, remove that row and
   re-import.** `aiacct remove <ACCOUNT_ID>`, then `aiacct import`. Expected
   observation: `import` prints the account it added and the path it came
   from, rather than "Added 0 accounts".

If you reach the bottom of this list with a clean `doctor` and a browser that
genuinely demands a password, and the vendor still returns account #1, the
problem is on the vendor's side of the OAuth exchange and this widget has no
lever on it.

---

## 4. Checking the state of things from the command line

The management tool is `tools/aiacct.py`. It uses only the Python standard
library, on purpose: a recovery tool must not be able to fail because a
dependency is missing, and it still runs when the widget itself will not.

```
python3 tools/aiacct.py <command>
```

Commands: `list`, `show`, `add`, `remove`, `rename`, `import`, `usage`,
`providers`, `doctor`, `repair`. Below, `aiacct` is shorthand for that line.

Account ids are printed shortened; **any unique prefix works** wherever an
`ACCOUNT_ID` is wanted, or pass `--full-ids` to `list` to see them whole. An
ambiguous prefix is rejected rather than resolved to the first match - picking
one would eventually remove the wrong account.

No command in this tool ever prints a credential value. `show` ends by saying
so explicitly.

### `aiacct list`

Prints a table with the columns `ID  PROVIDER  LABEL  EXPIRES  STATUS`, then a
count. An empty vault prints `No accounts registered.`, the vault path, and a
suggestion to run `aiacct import`.

The `STATUS` word is the thing to read. There are deliberately distinct words
for distinct situations, because one word - "expired" - being printed for
several unrelated conditions is the bug this whole rework exists to fix:

| STATUS | Meaning |
| --- | --- |
| `live` | The access token is valid right now. |
| `stale` | The access token has lapsed, but a refresh should work. |
| `unrecoverable` | Only a fresh browser login can fix it; no refresh can. |
| `planned` | The vendor is registered but has no usage endpoint, so the credential's state is moot. |
| `unknown` | The credential shape is unrecognised. **Not** a synonym for dead: we simply cannot tell. |
| `unknown-provider` | The provider slug is not in the registry at all. |

**A healthy result** for two Claude accounts: two rows, provider `claude`, two
distinct ids, status `live` - or `stale`, which is fine and self-healing - and
expiry times in the future.

**An unhealthy result:** `unrecoverable`, which means go and sign in again for
real; `unknown-provider`; or one row where you expected two. `unknown` on its
own is not a reason to redo a working login.

`list` also accepts `--provider`, `--json`, and `--watch SECONDS` for a
repeating render. `--watch` and `--json` cannot be combined, because a
repeating render is not a parseable JSON document.

### `aiacct show <ACCOUNT_ID>`

Detail for one account: id, provider and its registry status, label, when it
was added, where it came from, expiry, status. No secret material.

### `aiacct doctor`

The most useful command when something is wrong. It reports the vault path,
whether it exists, its permissions and size, files beside it, every registered
account, and the CLI credential files it can find on disk - naming any that are
**not** registered in the vault. It ends with either `No problems found.` or a
problem count.

Two of its warnings matter especially:

- The vault readable by other users on the system (non-Windows): it holds live
  refresh tokens and should be `0600`. On Windows the tool notes that the mode
  it prints is advisory and NTFS ACLs are what actually govern access.
- Credentials found on disk that are not in the vault. In the tool's own
  words: *this is the failure that masks a working subscription - a stale
  credential registered in the vault reports "expired" while a live one sits
  unregistered on disk. Run `aiacct import` to register them, then remove
  whichever duplicate is stale.*

It also flags leftover `.tmp` files beside the vault; `aiacct repair` deletes
those. `repair` is the one command here that removes things without being
pointed at them, so it describes each action and asks first. Pass `-y` to act
non-interactively; with no terminal to confirm on it does nothing rather than
guessing at consent.

### `aiacct import`

Scans the known CLI credential locations and registers anything not already in
the vault, leaving existing entries untouched. It distinguishes "every CLI
login found on this machine is already registered" from "no CLI credential
file was found in any known location", because a bare "0 accounts" reads like
a failure.

### `aiacct add <provider> <label> --from-file PATH`

Registers a credential from a file - for when the credential lives somewhere
non-standard, such as a WSL home or an encrypted volume. There is no headless
login here: the tool reads an existing credential file, it does not create
one. Log in with the vendor CLI first, then `import` or `add --from-file`.

Adding an account for a provider with no usage endpoint succeeds, and the tool
tells you plainly that it can be stored and listed but will report "not
supported yet" until a verified endpoint exists.

### `aiacct usage <ACCOUNT_ID>`

Queries the provider over the network and prints the quota figures. It
announces the call **before** making it - `Querying ... over the network...` -
so a request hanging against a silently dropped connection still leaves you
knowing what it is waiting on. For a provider with no verified endpoint it
refuses outright rather than pretending.

---

## 5. The two-copies problem

If a vendor CLI is installed both on Windows and inside WSL, the same login
exists twice - once in each home directory - and the two copies drift apart.
Whichever copy was used most recently holds the newer token. The vault holds
only one of them.

This produces the most misleading failure in the whole system: **a stale copy
registered in the vault reports a dead login while a perfectly live copy sits
unregistered on disk.** It looks exactly like a cancelled subscription. It is
not.

A stopped WSL distribution makes it worse: the copy inside it stops being
updated while still looking like a candidate, and promoting it yields "login
expired" for a login that is entirely healthy.

`aiacct doctor` is built for this. It lists the credential files it finds and
names the ones that are not registered. If usage fails for an account and
`doctor` shows another copy on disk, the registered one is probably the stale
one: `aiacct import` to register what is there, then remove whichever row is
stale - or point `add --from-file` at the copy you know is current.

The widget also drops credential copies older than the refresh-token lifetime
outright, rather than letting a long-dead copy win over a working one.

If you deliberately keep credentials outside the default locations, extra
paths can be supplied in `.env`; see `.env.example` for the exact variable
names and their separator.

---

## 6. Which providers actually work today

**Two.** Claude Code and OpenAI Codex.

| Provider | Usage reporting |
| --- | --- |
| Claude Code | Works - verified usage endpoint |
| OpenAI Codex / ChatGPT | Works - verified usage endpoint |
| Every other vendor in the picker | **Does not report usage.** Accounts can be stored and listed, nothing more. |

Ten or so other vendors are registered so their accounts can be kept in the
vault, and they appear in the provider picker. They have **no verified usage
endpoint**. Polling one returns `not supported yet`; `aiacct list` shows it as
`planned`; the widget's card says the provider is registered but has no usage
endpoint yet; and `aiacct usage` refuses with a message saying there is no
verified usage endpoint for it, so no quota can be read.

Being in the picker is not evidence that a provider works. Only Claude Code
and OpenAI Codex report quota.

---

## 7. The three failure states, and what each actually means

When a credential refresh fails, exactly one of three things is reported.
Conflating them is the bug this feature was written to eliminate.

**`login expired`** - the provider returned 400 or 401 *and* the response body
named the grant as expired. This is the only one that means what it says: go
and sign in again.

**`offline`** - the request never reached the provider at all. **This says
absolutely nothing about whether your login is valid.** A dropped VPN, a dead
link or blocked egress all land here. Do not re-sign-in on the strength of an
`offline`; fix the network and look again. Telling somebody their login died
when it did not is the precise failure this design exists to prevent.

**`refresh failed (N)`** - any other non-200 response, with the status code
kept. This is a provider-side problem, and the credential may well be fine. A
bare 400 is deliberately *not* reported as "login expired".

The same care applies to the status words in section 4: `unknown` means
undetermined, not dead.

---

## 8. The geographic gate

Quota lookups and logins are gated behind a geographic check, and the check
**fails closed** - if it cannot establish that your egress is acceptable, it
sends nothing. From a blocked region the widget refuses to poll and refuses to
sign in, typically saying `VPN required`.

**This is intended protection, not a malfunction.** An authentication request
is the single most visible thing a vendor sees from an address, and sending one
from a blocked region risks the account itself, not merely the request.
Refusing is the correct outcome.

What to do: bring up a VPN whose exit is in a region the vendors serve, then
retry. The verdict is cached briefly, so allow a moment after connecting.
There is also a hard latch that, once tripped, does not clear itself
automatically - if refusals persist after the VPN is clearly up, restart the
widget rather than hammering retry.

Do not try to route around the gate. It exists to protect your subscriptions.

---

## 9. Where credentials live, and what that means for safety

The widget's vault is a single file:

- Windows: `%APPDATA%\net-watch-ui\accounts.json`
- Elsewhere: `~/.config/net-watch-ui/accounts.json`

The environment variable `AI_ACCOUNTS_FILE` overrides that path outright.

Treat this file as a secret. **It holds live credentials, including refresh
tokens** - enough to act as your subscriptions.

`aiacct doctor` has an `== encryption at rest ==` section that classifies the
file as it exists right now, and warns in as many words if the tokens on disk
are readable. Do not assume it is encrypted; read what `doctor` says. It also
reports permissions: on non-Windows systems the vault should be `0600` and
`doctor` warns if other users on the machine can read it, while on Windows the
printed mode is advisory and NTFS ACLs are what actually govern access.

Two more things worth knowing:

- **During a login, a real credential exists briefly in a plain file inside
  the sandbox.** The sandbox is torn down unconditionally afterwards. If the
  widget was killed mid-login rather than cancelled, check for leftovers.
- **A surviving `.tmp` file beside the vault means an interrupted atomic
  write.** Those are extra copies of live credentials at rest. `aiacct doctor`
  flags them and `aiacct repair` deletes them once the vault itself reads
  correctly.

The tooling is built not to leak. Credential fields are rendered through an
explicit allowlist of safe keys rather than a blocklist of unsafe ones, so a
field added to a credential shape later cannot leak by default. Raw tracebacks
are never printed, because a traceback from a credential tool can put local
variables into your shell scrollback. No token, refresh token, or fragment of
one is printed in any mode or in any error message.

---

## What this document does not claim

- It does not claim a second account can be added without touching your
  browser. It cannot.
- It does not claim any provider beyond Claude Code and OpenAI Codex reports
  usage. None do.
- It does not claim the widget can prove two Claude credentials belong to two
  different email addresses. It compares non-secret credential fields, which
  is enough to spot the same login twice and not enough to assert identity.
