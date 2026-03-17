# Clean Code Skill

Principles for writing maintainable, readable software. Apply these to all code.

## The Golden Rule
Code is read 10x more than it is written. Write for the next person (which is usually you in 3 months).

## Naming
- **Variables**: describe what they contain (`user_count`, not `n` or `x`)
- **Functions**: describe what they DO (`calculate_total`, not `process` or `do_thing`)
- **Booleans**: use `is_`, `has_`, `can_`, `should_` prefix (`is_valid`, `has_errors`)
- **Classes**: noun phrases (`UserRepository`, not `HandleUsers`)
- Avoid abbreviations unless universally understood (`url`, `id`, `db` are fine)

## Functions
- **Single Responsibility**: one function = one thing. If you need "and" to describe it, split it.
- **Small**: ideally under 20 lines. 40 lines max before extracting sub-functions.
- **Pure when possible**: same input = same output, no hidden side effects
- **Max 3 parameters**: if you need more, use a dataclass or dict
- **Early returns** reduce nesting:

```python
# Bad (deeply nested)
def process(data):
    if data:
        if data.is_valid():
            if data.value > 0:
                return data.value * 2

# Good (early returns = flat)
def process(data):
    if not data:
        return None
    if not data.is_valid():
        return None
    if data.value <= 0:
        return None
    return data.value * 2
```

## DRY (Don't Repeat Yourself)
- If you write the same logic twice, extract it into a function
- If you write the same value in multiple places, make it a constant
- Three strikes rule: write it once → write it twice (ok) → third time = extract it

## Comments
**Write comments for WHY, not WHAT**. The code already says WHAT.

```python
# Bad — describes what the code does (obvious from reading it)
# Multiply price by 1.15 to add tax
total = price * 1.15

# Good — explains WHY (business logic that isn't obvious)
# UK VAT rate is 20%, but this client is in the reduced 15% bracket
total = price * 1.15
```

Remove comments that are:
- Outdated (disagree with the code)
- Obvious (`i += 1  # increment i`)
- Commented-out code (use git for history)

## Error Messages
Make error messages actionable:

```python
# Bad
raise ValueError("Invalid input")

# Good
raise ValueError(
    f"Expected temperature between -273 and 1000, got {temp}. "
    f"Check your sensor calibration."
)
```

## Magic Numbers
Never use unexplained numeric literals:

```python
# Bad
if len(items) > 50:
    throttle()

# Good
MAX_QUEUE_DEPTH = 50   # trigger backpressure above this
if len(items) > MAX_QUEUE_DEPTH:
    throttle()
```

## Structure
- Related code should be near each other
- Group: imports → constants → classes → functions → main logic
- Within a class: `__init__` → public methods → private methods (`_name`)
- Keep related test assertions in the same test, unrelated assertions in separate tests

## Code Smells to Avoid
- **Long parameter lists**: use a config object
- **Deep nesting** (>3 levels): extract functions, use early returns
- **God class**: one class doing everything — split by responsibility
- **Dead code**: if it's not used, delete it (git has history)
- **TODO comments**: either fix it now or create a task, don't leave it
- **Inconsistent naming**: pick a style and stick with it
