painfully-slow.nvim
---

A neovim plugin that emulates the rendering speed of the classic machines vim was built for.

# Background

I was able to get my Apple 2e to run as a dumb terminal to a shell process on my desktop. I ran vim and noticed how slow it actually was to move around and make edits. It immediately clicked why vim was so powerful at the time. Every time I went down 1 line at a time, the screen would almost completely re-render. Efficient motions become a necessity.

# What it does

The plugin makes the neovim screen render slowly -- character by character, top to bottom, left to right -- as if the terminal were connected to a 1 MHz processor over a serial line. The user watches the screen repaint the same way it would on an Apple 2e.

# Modes

The plugin has three states. The user can switch between them at any time.

## Off

Normal neovim. The plugin does nothing.

## Always slow

Every screen update renders at baud rate. Motions, edits, macros, plugin output, command results -- everything. No distinction between efficient and inefficient input. The terminal is just slow. This is for experiencing what vim actually felt like on the hardware it was designed for.

## Training

Only inefficient motions trigger slow rendering. Efficient motions render at full speed. The contrast between the two is the entire point -- the user feels the cost of inefficiency rather than being told about it.

### What counts as inefficient

Repeating a single-step motion when a more direct alternative exists. Examples:

- Pressing `j` seven times instead of `7j` or `}` or searching
- Pressing `w` five times instead of `5w` or `f` or `/`
- Pressing `h` and `l` repeatedly instead of `0`, `^`, `$`, `f`, or `t`
- Deleting lines one at a time instead of a range delete
- Deleting characters one at a time instead of `dw` or `dt`

### How severity scales

The more wasteful the motion, the slower the render. A small repeated sequence gets a brief flicker of delay. A long one gets the full 300-baud experience where you can read each character as it appears on screen.

### Hints

After a slow render, the plugin can show the user what the efficient alternative would have been.

# Invariants

These must always be true regardless of mode or configuration.

1. The user can toggle between off, training, and always-slow at any time during a session.
2. In always-slow mode, there are no exceptions. Every screen update is slow. Macros, plugin output, automated sequences -- all of it.
3. In training mode, only human-initiated inefficient motions trigger slow rendering. Automated or programmatic sequences are never penalized.
4. In training mode, efficient motions always render at full speed. There is never a delay on a counted motion, search, mark, jump, or text object.
5. The slow rendering is visible -- the user sees characters appearing on screen over time, not a frozen screen followed by a jump. The render must look like a real terminal redraw.
6. The amount of screen that changed determines how long the slow render takes. More change means more visible repaint time, exactly like real hardware.
7. The user can always interrupt a slow render in progress.
8. When the plugin is off, it has zero effect on neovim behavior or performance.
9. The rendering speed is configurable. The user chooses the baud rate.
10. In training mode, the threshold for what counts as "repeated" is configurable. The user decides how many repeated single-step motions are tolerable before penalty.

# Open questions

- Should there be a stats or progress view showing motion efficiency over time?
- Is there value in a "ghost mode" that shows the slow render in a separate window rather than the main buffer?
