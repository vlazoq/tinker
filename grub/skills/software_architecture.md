# Software Architecture Skill

Architectural patterns and principles to apply when implementing systems.

## SOLID Principles

**S — Single Responsibility**: A class/module should have ONE reason to change.
- Good: `UserRepository` handles only DB access for users
- Bad: `UserManager` handles DB access + email sending + PDF generation

**O — Open/Closed**: Open for extension, closed for modification.
- Add new behaviour by adding new classes, not changing existing ones
- Use interfaces/base classes so new implementations can be dropped in

**L — Liskov Substitution**: Subclasses must be usable wherever the parent is used.
- If `Animal` has a `speak()` method, every subclass must work when passed as `Animal`

**I — Interface Segregation**: Many small interfaces > one large interface.
- Don't force classes to implement methods they don't need

**D — Dependency Inversion**: Depend on abstractions, not concrete implementations.
- Pass dependencies as constructor arguments, don't import them directly inside classes
- This makes testing easy: pass a mock instead of the real thing

## Dependency Injection (Most Important Pattern)

```python
# BAD — hard-coded dependency (impossible to test or swap)
class OrderProcessor:
    def process(self, order):
        db = PostgresDatabase("prod-server")   # can't test without a real DB!
        email = SMTPEmailSender()              # can't test without an SMTP server!
        db.save(order)
        email.send(order.user_email, "Order confirmed")

# GOOD — injected dependencies (testable, swappable)
class OrderProcessor:
    def __init__(self, db: Database, emailer: EmailSender) -> None:
        self.db      = db
        self.emailer = emailer

    def process(self, order):
        self.db.save(order)
        self.emailer.send(order.user_email, "Order confirmed")

# In tests:
processor = OrderProcessor(db=FakeDatabase(), emailer=FakeEmailSender())
# In production:
processor = OrderProcessor(db=PostgresDatabase(...), emailer=SMTPEmailSender(...))
```

## Common Patterns

### Repository Pattern (data access)
Separate "how data is stored" from "business logic":
```python
class TaskRepository:
    def save(self, task: Task) -> None: ...
    def find_by_id(self, id: str) -> Optional[Task]: ...
    def find_pending(self) -> list[Task]: ...

class TaskService:
    def __init__(self, repo: TaskRepository) -> None:
        self.repo = repo
    def complete_task(self, task_id: str) -> None:
        task = self.repo.find_by_id(task_id)
        task.mark_complete()
        self.repo.save(task)
```

### Strategy Pattern (swappable algorithms)
```python
class SortStrategy(ABC):
    @abstractmethod
    def sort(self, items: list) -> list: ...

class QuickSort(SortStrategy):
    def sort(self, items): ...

class MergeSort(SortStrategy):
    def sort(self, items): ...

class DataProcessor:
    def __init__(self, sorter: SortStrategy) -> None:
        self.sorter = sorter
    def process(self, data):
        return self.sorter.sort(data)
```

### Factory Pattern (object creation)
```python
def create_storage(storage_type: str) -> StorageBackend:
    if storage_type == "redis":
        return RedisStorage(url=os.getenv("REDIS_URL"))
    elif storage_type == "sqlite":
        return SQLiteStorage(path=os.getenv("DB_PATH"))
    raise ValueError(f"Unknown storage type: {storage_type}")
```

## Layered Architecture
Structure code in layers, each only depending on the layer below:

```
API / UI Layer         (HTTP handlers, CLI, web UI)
         ↓ calls
Service Layer          (business logic, workflows)
         ↓ calls
Repository Layer       (data access — DB, files, cache)
         ↓ calls
Infrastructure Layer   (SQLite, Redis, HTTP clients, file system)
```

- Each layer only imports from the layer directly below
- Business logic never imports HTTP or SQLite directly
- This makes each layer independently testable

## Avoid These Anti-Patterns

**God Object**: One class that does everything.
→ Split by responsibility.

**Spaghetti Code**: Tangled dependencies, circular imports.
→ Draw the dependency graph. It should be a DAG (no cycles).

**Premature Optimisation**: Making code faster before it's known to be slow.
→ Write clear code first. Profile. Optimise only what's measured as slow.

**Copy-Paste Programming**: Duplicating logic instead of abstracting.
→ DRY. Extract.

**Stringly Typed**: Using strings where enums or typed objects should be used.
→ Use `TaskStatus.PENDING` not `"pending"`.
