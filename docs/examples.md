# Practical Examples

[← docs index](index.md)

This page provides practical, real-world examples of how you can integrate `quarantine` into different workflows, and exactly what gets created behind the scenes when a failure happens.

## 1. The Basic Data Pipeline

The most common use case: processing a large list of items (like a CSV or a database query) where you cannot afford the entire job to crash because of one malformed row.

```python
from quarantine import quarantine


# 1. Simply decorate the function that processes a single item
@quarantine
def process_user(user_data: dict):
    # If this fails (e.g., missing 'email' key), the loop won't crash.
    email = user_data["email"].lower()
    # ... process and save ...


def main():
    users = [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob"},  # Missing email! This will fail.
        {"name": "Charlie", "email": "charlie@example.com"},
    ]

    for u in users:
        process_user(u)


if __name__ == "__main__":
    main()
```

**What happens?**
Instead of the script dying on Bob, it prints a clean stderr summary at the end:
```text
✓ 2 processed · ✗ 1 quarantined → .quarantine/
```

## 2. Asynchronous Web Scraping

`quarantine` works flawlessly with `async def`. This is perfect for when you are hammering an API or scraping websites, and random timeout or parsing errors occur.

```python
import asyncio
from quarantine import quarantine


@quarantine
async def fetch_and_parse(url: str):
    # Imagine hitting a 500 error or a malformed DOM here
    raise ValueError(f"Failed to parse {url}")


async def main():
    urls = ["https://example.com/1", "https://example.com/2"]

    # Run them all concurrently
    tasks = [fetch_and_parse(url) for url in urls]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
```

## 3. Redacting Secrets

If you process sensitive data (API keys, passwords, PII), you don't want them written to disk inside the `.quarantine/` folder for anyone to see. Use `redact`.

```python
from quarantine import quarantine


@quarantine(redact=["password", "api_key"])
def login(payload: dict):
    # The payload is saved if it crashes, but the secret values are scrubbed!
    pass
```

## 4. Shielding a Block of Code (No Decorator)

Sometimes you don't want to extract a chunk of logic into a separate function just to decorate it. You can use `shield()` directly inside a loop.

```python
from quarantine import shield


def process_batch(batch_of_items):
    # Shield iterates over the items safely
    for item in shield(batch_of_items, using=process_batch):
        if item == "bad":
            raise RuntimeError("Corrupted item")
        print(f"Processed {item}")
```

## 5. The Local Web Dashboard

If you have accumulated several errors and want to inspect them comfortably, you can spin up a local UI dashboard without installing any extra dependencies:

```bash
quarantine ui
```

Opening `http://localhost:8080` in your browser will display a clean table of all failures. You can click into individual records to view the syntax-highlighted `input.json` and tracebacks, and even click **Retry** directly from the UI once you have fixed the underlying bug.


## What Gets Created? (The File Structure)

When an item is quarantined, a new `.quarantine/` directory is created in your current working directory. The structure looks like this:

```text
.quarantine/
├── .index.json                 # Fast lookup table for CLI commands
├── .index.lock                 # Concurrency lock
└── fb2a1/                      # Unique ID for the failure
    ├── input.json              # The arguments passed to your function
    ├── traceback.txt           # The full Python stack trace
    └── meta.json               # Timestamp, function name, and machine details
```

### Inside `input.json`
If you passed `{"name": "Bob"}` to your function, `quarantine` serializes it beautifully:

```json
{
  "args": [
    {
      "name": "Bob"
    }
  ],
  "kwargs": {}
}
```
*(If your input isn't JSON-serializable, it falls back to a readable `input.txt` representation!)*

### Inside `traceback.txt`
You'll see exactly what went wrong without needing logs:
```text
Traceback (most recent call last):
  File "job.py", line 7, in process_user
    email = user_data["email"].lower()
KeyError: 'email'
```

### Dealing with the failures
Once you fix the code in `job.py`, you simply run:
```bash
quarantine retry -i job.py
```
And it reads `input.json`, re-imports your function, and processes Bob successfully!
