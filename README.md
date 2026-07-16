# 🎅 Secret Santa 🎁

Ditch the crumpled pieces of paper and let code do the festive matchmaking!

This script generates Secret Santa gift assignments from a simple JSON list of your participants. It handles the tricky stuff, like exclusions so partners or siblings don't accidentally draw each other (saving the surprise for the big day!).

Requires Python 3.14+ (Santa's elves only use the latest tech). 🐍🧝‍♂️ [uv](https://docs.astral.sh/uv/) automatically fetches it for you if you don't have it.


## 🛷 Usage

Run the script with [uv](https://docs.astral.sh/uv/), it sets up the environment automatically on first run:

```
uv run secret_santa.py participants.json
uv run secret_santa.py participants.json --format json
uv run secret_santa.py participants.json --format html
uv run secret_santa.py participants.json --per-person ./messages
uv run secret_santa.py participants.json --seed 42

```

### 🎛️ Options


| Flag | Description |
| --- | --- |
| `-f`, `--format` | Output format: `table` (default), `json`, or `html` |
| `-o`, `--per-person DIR` | Also write one HTML message per giver into `DIR` |
| `--seed` | Integer seed for a reproducible draw (testing only) |

## 🛠️ Santa's Workshop (Development)

The project uses [uv](https://docs.astral.sh/uv/) to manage the environment and dev tools.
[Install uv](https://docs.astral.sh/uv/getting-started/installation/) once, then:

```
uv sync
```

That's it — `uv sync` creates `.venv`, installs Python 3.14 if you don't have it
(the project uses `string.templatelib` and PEP 758 `except` syntax), and installs the
locked dev dependencies (pytest, ruff, ty, pre-commit) from `uv.lock`. No manual
venv creation or activation needed; `uv run` executes commands inside the project
environment.

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock` — after changing
them, run `uv lock` and commit the updated lockfile.

Run the test suite from the repo root to ensure the elves are working hard:

```
uv run pytest        # discovers tests/ via pyproject.toml
uv run pytest -v     # verbose
```

### 🧹 Tidying the Workshop (lint, format & type-check)

[Ruff](https://docs.astral.sh/ruff/) handles both linting and formatting; [ty](https://docs.astral.sh/ty/)
type-checks the code. Configuration lives in `pyproject.toml`.

```
uv run ruff check              # lint
uv run ruff check --fix        # lint and auto-fix what it safely can
uv run ruff format             # format the code
uv run ruff format --check     # verify formatting without writing (what CI runs)
uv run ty check                # type-check the source and tests (config picks the files)
```

> ty checks both `secret_santa.py` and the `tests/` suite (`[tool.ty.src]`), so keep new
> tests fully type-annotated. Suppress a specific diagnostic with `# ty: ignore[rule-name]`.

### Pre-commit hooks

Install the hooks once and they run ruff (lint + format) and ty automatically on every commit:

```
uv run pre-commit install                 # enable the hooks
uv run pre-commit run --all-files         # run them across the whole repo on demand
```

### Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs ruff, ty, and pytest on every push and pull request.

## 📜 The Naughty & Nice List (Participant Format)

A JSON file containing a list of participants, each with a `name`, an optional `email`, and an optional list of `excludes` (names this person must not be assigned to give a gift to):

```json
[
  { "name": "Santa Claus", "email": "santa@northpole.com", "excludes": ["Mrs. Claus"] },
  { "name": "Mrs. Claus",  "email": "mary.claus@northpole.com", "excludes": ["Santa Claus"] },
  { "name": "Rudolph",     "email": "rudolph@northpole.com" },
  { "name": "Hermey",      "email": "hermey@dentistry.com" },
  { "name": "The Grinch",  "email": "grinch@mtcrumpit.com", "excludes": ["Max"] },
  { "name": "Max",         "email": "max@mtcrumpit.com", "excludes": ["The Grinch"] }
]
```

See `participants.example.json` for a working sample.

## ✨ How the Magic Works

`secret_santa.py` produces a random derangement (nobody draws themselves) that respects every `excludes` constraint. It prefers a single cycle — one continuous chain of gift-giving that loops back to the start so there are no reciprocal "you drew me, I drew you" pairs. If exclusions make a single cycle impossible, it falls back to any valid derangement rather than failing. It only raises an error when no valid assignment exists at all.

## 🎁 Wrapping It Up (Output)

Choose how you want to receive your gift assignments:

* `table` a plain-text list of giver → receiver pairs.
* `json` a `{"giver": "receiver"}` mapping.
* `html` a short HTML page per giver announcing who they drew. Outputting directly to `stdout` (`-f html`) is especially useful for:
  * **Automation & Piping:** Pass the generated HTML directly to a mail utility (e.g., `uv run secret_santa.py participants.json -f html | sendmail`).
  * **Master Print Sheets:** Redirect the output to a single file (e.g., `... -f html > master_sheet.html`) to easily print out everyone's cards at once on physical paper.
  * **Web Integration:** Easily capture the standard output to render directly on a webpage or web application.

Pass `--per-person DIR` to additionally write each giver's HTML note to its own file in `DIR`, making it perfectly suited for sending or sharing privately with each participant!

All output, stdout and the per-person files, is always UTF-8, regardless of the system locale. (The participants file must be UTF-8 too.)