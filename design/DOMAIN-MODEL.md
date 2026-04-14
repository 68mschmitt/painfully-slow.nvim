# painfully-slow.nvim — Domain Model

This document defines the domain model for painfully-slow.nvim. It was derived from the [plugin spec](./painfully-slow-2026-04-14_09-28-12--note.md) through structured expert consultation before any code was written.

The model is organized around the plugin's 10 invariants. Every data type, function, and adapter exists to enforce one or more of these invariants.

---

## Design Decisions

These decisions were made during domain modeling and are not open for renegotiation during implementation.

### 1. Pure domain logic is completely separated from Neovim APIs

The classifier, severity scaler, hint generator, render planner, and state machine never call `vim.*` anything. They take plain Lua values and return plain Lua values. They are testable in plain Lua without a running Neovim instance.

The Neovim API is an adapter layer at the boundary. It translates between Neovim events and domain data.

### 2. Source tagging happens at capture time

Every motion event is stamped `human` or `programmatic` at the moment it enters the system. This is determined by `vim.on_key`'s `typed` parameter: if `typed` is empty/nil, the event is programmatic. This decision is made once, at the boundary, and travels with the event as data. It is never inferred later.

### 3. Mode is data, not scattered conditionals

The current mode is a single value: `"off"`, `"always_slow"`, or `"training"`. It parameterizes pure functions. There are no `if mode == "training" then ...` branches scattered through the codebase. Mode is read once at the top of the event pipeline and threaded through as an argument.

### 4. Rendering is a simulation via floating window overlay

Neovim does not expose control over its terminal renderer. The "slow character-by-character rendering" is achieved by:
1. Letting the real buffer update happen normally (instantaneously)
2. Covering the screen with a full-screen floating window showing the *previous* state
3. Animating the new content into the overlay character by character using `vim.uv.new_timer()`
4. Closing the overlay when the animation completes, revealing the already-correct buffer underneath

This is an illusion. The buffer is always in the correct state. Only the *visible presentation* is delayed.

### 5. The renderer is non-blocking and returns a cancellable handle

The animation loop uses `vim.uv.new_timer()` (not a blocking loop, not `vim.fn.getchar()`). It returns a handle with a `cancel()` method. Any keypress during animation sets an interrupt flag checked on each timer tick.

### 6. Invalid states are made unrepresentable through constructor functions

Data types are plain Lua tables, but they are only created through factory functions that enforce representation invariants. Direct table construction is not used outside of factories.

### 7. Five separate concerns, five separate identities

A naive implementation would put everything in one mutable state blob. This model keeps five things with different change semantics strictly separate:
- **Config** — changes rarely, by explicit user action
- **Mode** — changes on user command
- **Motion history** — changes on every keystroke
- **Active render** — changes at baud rate during animation
- **Classification state** — changes per motion sequence evaluation

---

## Data Types

### MotionEvent

A fact about a single keypress or command. Immutable once created.

```lua
-- Factory: motion_event.create(key, count, motion_class, source, timestamp)
{
  key          = "j",          -- raw key sequence
  count        = nil,          -- explicit count prefix (nil = no count given)
  motion_class = "line_down",  -- semantic classification
  source       = "human",      -- "human" | "programmatic" (set at capture, never changed)
  timestamp    = 1744567890,   -- monotonic ms from vim.uv.now()
}
```

**Representation invariant:**
- `source` ∈ `{"human", "programmatic"}`
- `count` is nil or a positive integer
- `timestamp` is monotonically increasing within a session

**Invariants enforced:** #3 (source field distinguishes human from programmatic)

---

### MotionVerdict

The result of classifying a motion sequence. Immutable once created.

```lua
-- Factory (efficient): motion_verdict.efficient()
{
  inefficient  = false,
  waste_factor = 0,      -- always 0 when efficient
  repeat_count = 0,      -- always 0 when efficient
  hint         = nil,     -- always nil when efficient
}

-- Factory (inefficient): motion_verdict.inefficient(waste_factor, repeat_count, hint)
{
  inefficient  = true,
  waste_factor = 0.7,    -- 0.0 to 1.0, how wasteful
  repeat_count = 7,      -- how many redundant steps
  hint         = "7j",   -- the efficient alternative
}
```

**Representation invariant:**
- If `inefficient == false`, then `waste_factor == 0` and `repeat_count == 0` and `hint == nil`
- If `inefficient == true`, then `waste_factor > 0` and `repeat_count > 0` and `hint ~= nil`
- `waste_factor` ∈ [0.0, 1.0]

**Invariants enforced:** #4 (efficient verdicts guarantee no penalty by construction)

---

### RenderPlan

Describes what to render and how fast. Immutable once created.

```lua
-- Factory (immediate): render_plan.immediate()
{
  strategy  = "immediate",
  baud_rate = nil,       -- always nil when immediate
  char_count = 0,
  hint      = nil,
}

-- Factory (throttled): render_plan.throttled(baud_rate, char_count, hint)
{
  strategy   = "throttled",
  baud_rate  = 300,       -- characters per second after 8N1 encoding
  char_count = 847,       -- characters to paint
  hint       = "7j",      -- nil if no hint or hints disabled
}
```

**Representation invariant:**
- If `strategy == "immediate"`, then `baud_rate == nil`
- If `strategy == "throttled"`, then `baud_rate > 0` and `char_count > 0`
- All throttled plans are interruptible (invariant 7 — no flag needed, it's universal)

**Invariants enforced:** #2 (always-slow produces throttled unconditionally), #6 (char_count drives duration), #8 (off mode produces immediate unconditionally)

---

### RenderProgress

The in-flight state of an active animation. The only high-frequency mutable state in the system.

```lua
{
  plan          = <render_plan>,   -- the plan being executed
  chars_painted = 312,             -- how far we've gotten
  started_at    = 1744567890,      -- when the animation began
}
```

**Invariants enforced:** #5 (chars_painted advances incrementally — visible, not frozen), #7 (setting active render to nil cancels it)

---

### Config

User preferences. Changes rarely and only by explicit user action.

```lua
{
  baud_rate         = 300,       -- base baud rate for maximum-waste render
  repeat_threshold  = 3,         -- repeated motions before penalty triggers
  hints_enabled     = true,      -- show hints after slow render
  severity_curve    = "linear",  -- "linear" | "exponential"
}
```

**Invariants enforced:** #9 (baud_rate is configurable), #10 (repeat_threshold is configurable)

---

## Pure Functions

These functions take data and return data. They have no side effects. They never call `vim.*`. They receive everything they need as arguments.

### classify_motion

```
classify_motion(history: MotionEvent[], config: Config) → MotionVerdict
```

Given a window of recent motion events and the current config, produce a verdict.

- Filters out events where `source == "programmatic"` before analysis → **enforces invariant #3**
- If remaining events don't exceed `config.repeat_threshold` consecutive identical single-step motions, returns `motion_verdict.efficient()` → **enforces invariant #4**
- If they do, returns `motion_verdict.inefficient(waste_factor, count, hint)` where `waste_factor` scales with repeat count → **enforces invariant #10**

**Design note:** This function operates on a *window* of recent events, not a single event. The window size and expiry semantics are implementation details, but the function is always pure — old history in, verdict out.

---

### plan_render

```
plan_render(verdict: MotionVerdict, mode: string, char_count: number, config: Config) → RenderPlan
```

Given a verdict, the current mode, the number of characters that changed on screen, and the config, produce a render plan.

- If `mode == "off"`, returns `render_plan.immediate()` unconditionally → **enforces invariant #8**
- If `mode == "always_slow"`, returns `render_plan.throttled(config.baud_rate, char_count, nil)` unconditionally → **enforces invariant #2**
- If `mode == "training"` and `verdict.inefficient == false`, returns `render_plan.immediate()` → **enforces invariant #4**
- If `mode == "training"` and `verdict.inefficient == true`, returns `render_plan.throttled(scaled_rate, char_count, hint)` → **enforces invariants #6, #9**

The `scaled_rate` is computed by `scale_baud_rate(verdict.waste_factor, config)`.

**Design note:** Mode dispatch happens here and only here. No other function inspects the mode.

---

### scale_baud_rate

```
scale_baud_rate(waste_factor: number, config: Config) → number
```

Maps a waste factor (0.0–1.0) to a baud rate between `config.baud_rate` (slowest, max waste) and full speed (no waste). The mapping follows `config.severity_curve`.

---

### derive_hint

```
derive_hint(verdict: MotionVerdict, config: Config) → string | nil
```

If `verdict.inefficient == true` and `config.hints_enabled == true`, returns a formatted hint string. Otherwise returns nil. Independent of rendering — the hint is derived from the verdict, not from the render progress.

---

### update_history

```
update_history(history: MotionEvent[], event: MotionEvent, config: Config) → MotionEvent[]
```

Given the current motion history, a new event, and config, returns a new history window. Handles windowing/expiry (e.g., drop events older than N seconds or beyond a max window size). Pure — old history in, new history out.

---

## Neovim Adapters

These are the side-effectful boundary modules that translate between Neovim and the pure domain.

### KeyWatcher

**Mechanism:** `vim.on_key(callback, namespace_id)`

**Responsibility:**
1. Receives raw keystrokes from Neovim
2. Tags each event with `source = "human"` or `source = "programmatic"` based on `vim.on_key`'s `typed` parameter (if `typed` is empty/nil → programmatic)
3. Reads `vim.v.count` to detect count prefixes
4. Creates `MotionEvent` values via the factory function
5. Feeds events to the pure domain pipeline

**Invariants enforced:** #3 (source tagging at boundary)

**Edge cases handled:**
- Macros (`@q`): replayed keys have empty `typed` → correctly tagged as programmatic
- Dot repeat (`.`): replayed keys have empty `typed` → correctly tagged as programmatic
- Count prefixes (`7j`): detected via `vim.v.count` → event gets `count = 7`
- Operator-pending (`d3j`): detected via `vim.api.nvim_get_mode()` or `ModeChanged` autocmd

---

### Renderer

**Mechanism:** Floating window overlay + `vim.uv.new_timer()` + `vim.schedule_wrap`

**Responsibility:**
1. Captures current visible buffer content (the "after" state)
2. Creates a full-screen floating window initialized with the "before" state
3. Runs a timer that advances one character per tick, writing to the overlay
4. Checks an interrupt flag on each tick
5. On completion or interrupt: closes the float, revealing the already-correct buffer

**Invariants enforced:** #5 (visible character-by-character rendering), #7 (interrupt via any keypress)

**Interrupt mechanism:** A second `vim.on_key` handler in a separate namespace sets an `interrupted` flag. The timer loop checks this flag. Any keypress interrupts — the user doesn't need a special key.

**Timer math:** At baud rate B with 8N1 encoding, characters per second = B / 10. Delay per character = 1000 / (B / 10) ms. At 300 baud → ~33ms per character. At 9600 baud → ~1ms per character (below reliable timer resolution — batch characters per tick at high rates).

---

### ModeController

**Mechanism:** A single Lua variable + user commands (`:PainfullySlowOff`, `:PainfullySlowTraining`, `:PainfullySlowAlwaysSlow`, `:PainfullySlowToggle`)

**Responsibility:** Holds the current mode value. Exposes `get()` and `set(mode)`. On mode change to `"off"`, triggers teardown. On mode change from `"off"`, triggers setup.

**Invariants enforced:** #1 (toggle at any time — all transitions are valid)

---

### Teardown

**Mechanism:** `vim.on_key(nil, namespace)` to remove hooks, `vim.api.nvim_del_augroup_by_id()` to remove autocmds, `renderer.cancel()` to stop active animation.

**Invariants enforced:** #8 (zero effect when off — all hooks removed, no listeners registered, no timers running)

---

## Invariant Enforcement Map

| # | Invariant | Enforcement | Where |
|---|-----------|-------------|-------|
| 1 | Toggle between modes at any time | All transitions valid, no guards | `ModeController` |
| 2 | Always-slow: no exceptions | `plan_render` returns throttled unconditionally when mode is always_slow | `plan_render` |
| 3 | Programmatic sequences never penalized | Source tagged at capture; `classify_motion` filters out programmatic events | `KeyWatcher` + `classify_motion` |
| 4 | Efficient motions always full speed | `motion_verdict.efficient()` makes penalty unrepresentable; `plan_render` returns immediate | `MotionVerdict` factory + `plan_render` |
| 5 | Rendering is visible, not frozen-then-jump | Timer-based character-by-character overlay animation | `Renderer` |
| 6 | Screen change amount determines duration | `char_count` passed to `render_plan.throttled`; duration = char_count / cps | `plan_render` + `RenderPlan` |
| 7 | User can always interrupt | Any-keypress interrupt flag checked each timer tick | `Renderer` |
| 8 | Off = zero effect | `plan_render` returns immediate; all hooks removed on teardown | `plan_render` + `Teardown` |
| 9 | Baud rate configurable | Config field, read by `scale_baud_rate` and `plan_render` | `Config` + `scale_baud_rate` |
| 10 | Repeat threshold configurable | Config field, read by `classify_motion` | `Config` + `classify_motion` |

---

## Module Structure

```
painfully-slow.nvim/
├── plugin/
│   └── painfully-slow.lua            # Entry point: user commands only, no logic
├── lua/
│   └── painfully-slow/
│       ├── init.lua                   # Public API: setup(), enable(), disable(), toggle()
│       ├── config.lua                 # Config factory + defaults + validation
│       ├── state.lua                  # Mode state: get(), set(), toggle()
│       ├── types/
│       │   ├── motion_event.lua       # MotionEvent factory + invariant checks
│       │   ├── motion_verdict.lua     # MotionVerdict factory (efficient/inefficient)
│       │   └── render_plan.lua        # RenderPlan factory (immediate/throttled)
│       ├── classify.lua               # classify_motion() — pure
│       ├── plan.lua                   # plan_render(), scale_baud_rate() — pure
│       ├── hints.lua                  # derive_hint() — pure
│       ├── history.lua                # update_history() — pure
│       ├── adapters/
│       │   ├── key_watcher.lua        # vim.on_key hook, source tagging
│       │   └── renderer.lua           # Floating window overlay, timer animation
│       └── dispatcher.lua             # The thin coordinator: reads state, calls pure fns, triggers adapters
├── tests/
│   ├── classify_spec.lua              # Pure Lua tests for classifier
│   ├── plan_spec.lua                  # Pure Lua tests for render planner
│   ├── types_spec.lua                 # Factory invariant tests
│   ├── history_spec.lua               # History window tests
│   └── integration/
│       ├── always_slow_spec.lua       # Neovim integration tests
│       └── training_spec.lua          # Neovim integration tests
├── doc/
│   └── painfully-slow.txt             # Vimdoc
└── README.md
```

---

## Build Sequence

Each step produces something testable and, from Step 4 onward, something demonstrable.

| Step | What | Tests | Deliverable |
|------|------|-------|-------------|
| 1 | Type factories + `classify_motion` | Pure Lua: j×5 → inefficient, 5j → efficient, threshold config | Classifier works in isolation |
| 2 | `state.lua` + `plan_render` + `dispatcher` | Pure Lua: mode transitions, render plan dispatch | Policy logic fully tested |
| 3 | `scale_baud_rate` + `derive_hint` | Pure Lua: waste→speed mapping, hint generation | All pure functions complete |
| 4 | `key_watcher` + dispatcher wired to print | Neovim: press jjjjj, see verdict in command line | Walking skeleton — concept proven |
| 5 | `renderer` (always-slow mode) | Neovim: every keystroke renders slowly | Always-slow mode works |
| 6 | Training mode end-to-end | Neovim: jjjjj is slow, 5j is fast | **MVP — the core teaching experience** |
| 7 | Hints | Neovim: after slow render, see "try 5j" | Teaching moment complete |
| 8 | Polish: config, commands, docs, README | Full plugin ready for installation | **v1.0** |

---

## Open Design Questions (Deferred)

These emerged during modeling but are intentionally deferred to implementation time:

1. **Sliding window vs. fixed sequence for classify_motion** — Should the history window be time-based (drop events older than N ms) or count-based (keep last N events)? Try time-based first; the feel of "rapid jjjjj" vs "j ... pause ... j" matters for UX.

2. **Screen diff granularity** — The spec says "amount of screen change." Start simple: count changed lines × average line length. Refine to character-level diffing only if the line-level approximation feels wrong.

3. **Syntax highlighting in overlay** — Skip for v1. The pedagogical value is in the delay, not the colors. Plain text overlay is honest and ships faster.

4. **Stats/progress view** — Listed as an open question in the spec. Defer entirely until the core experience is validated.

5. **Ghost mode** — Listed as an open question in the spec. Defer entirely.
