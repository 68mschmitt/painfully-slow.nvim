--- painfully_slow_poc.lua
---
--- Proof of concept: slow character-by-character terminal rendering.
--- Validates the floating-window overlay technique from the domain model.
---
--- Usage:
---   :luafile poc.lua            Load (idempotent, safe to re-source)
---   :PainfullySlowStart         Enable slow rendering
---   :PainfullySlowStop          Disable
---   :PainfullySlowToggle        Toggle
---
--- Keys typed during a slow render are queued and replayed one at a
--- time, each triggering its own slow render. This is how real slow
--- hardware behaved: vim processed input after the screen caught up.
---
--- Ctrl-C aborts the current render and clears the queue.
--- Change BAUD_RATE below to adjust speed.

----------------------------------------------------------------------
-- Config
----------------------------------------------------------------------

local BAUD_RATE = 150000 -- 300 = Apple 2e over serial. 2400 = tolerable. 9600 = fast.

----------------------------------------------------------------------
-- Idempotent cleanup (safe to :luafile multiple times)
----------------------------------------------------------------------

if _G.__painfully_slow_poc and _G.__painfully_slow_poc.stop then
  pcall(_G.__painfully_slow_poc.stop)
end

----------------------------------------------------------------------
-- State
----------------------------------------------------------------------

local M = {}

local enabled = false
local animating = false
local prev_screen = nil -- { lines: string[], width: int, height: int }
local overlay = { buf = nil, win = nil }
local key_queue = {}
local augroup = nil

----------------------------------------------------------------------
-- Screen capture
--
-- Returns the visible content of the current window as a table of
-- strings including the line number column. Each line is padded to
-- the full window width (text area + decorations) so the overlay
-- covers the number column too.
----------------------------------------------------------------------

local function capture_screen()
  local win = vim.api.nvim_get_current_win()
  local buf = vim.api.nvim_win_get_buf(win)
  local text_width = vim.api.nvim_win_get_width(win)
  local height = vim.api.nvim_win_get_height(win)

  -- textoff = total columns used by sign, number, and fold columns
  local wininfo = vim.fn.getwininfo(win)[1]
  local textoff = wininfo.textoff
  local full_width = text_width + textoff

  local top = vim.fn.line("w0")
  local bot = vim.fn.line("w$")
  local cursor_line = vim.fn.line(".")
  local line_count = vim.api.nvim_buf_line_count(buf)

  local buf_lines = vim.api.nvim_buf_get_lines(buf, top - 1, bot, false)

  -- Number column metrics
  local has_number = vim.wo[win].number
  local has_relnumber = vim.wo[win].relativenumber
  local num_col_width = 0
  local num_display_width = 0
  local gutter_pad = 0 -- sign/fold columns before the number column

  if has_number or has_relnumber then
    num_col_width = math.max(vim.wo[win].numberwidth, #tostring(line_count) + 1)
    num_display_width = num_col_width - 1
    gutter_pad = math.max(0, textoff - num_col_width)
  else
    gutter_pad = textoff
  end

  -- Build display lines with number prefix
  local lines = {}
  for i = 1, #buf_lines do
    local lnum = top + i - 1
    local prefix

    if has_number or has_relnumber then
      local display_num
      if has_relnumber and has_number then
        display_num = (lnum == cursor_line) and lnum or math.abs(lnum - cursor_line)
      elseif has_relnumber then
        display_num = math.abs(lnum - cursor_line)
      else
        display_num = lnum
      end
      prefix = string.rep(" ", gutter_pad)
        .. string.format("%" .. num_display_width .. "d", display_num)
        .. " "
    else
      prefix = string.rep(" ", textoff)
    end

    local full_line = prefix .. buf_lines[i]
    if #full_line < full_width then
      full_line = full_line .. string.rep(" ", full_width - #full_line)
    elseif #full_line > full_width then
      full_line = full_line:sub(1, full_width)
    end
    lines[i] = full_line
  end

  -- Pad to window height (tilde lines below buffer content)
  while #lines < height do
    local tilde = string.rep(" ", textoff) .. "~"
    if #tilde < full_width then
      tilde = tilde .. string.rep(" ", full_width - #tilde)
    end
    lines[#lines + 1] = tilde
  end
  while #lines > height do
    lines[#lines] = nil
  end

  return { lines = lines, width = full_width, height = height }
end

----------------------------------------------------------------------
-- Helpers
----------------------------------------------------------------------

local function screen_changed(a, b)
  if not a or not b then return true end
  if #a.lines ~= #b.lines then return true end
  for i = 1, #a.lines do
    if a.lines[i] ~= b.lines[i] then return true end
  end
  return false
end

--- Drain all pending terminal input into key_queue.
--- Returns true if Ctrl-C / interrupt was detected (signals abort).
local function drain_input()
  while true do
    local ok, c = pcall(vim.fn.getcharstr, 0)
    if not ok then
      -- pcall failure = Ctrl-C set the got_int flag, which makes
      -- getcharstr throw instead of returning a character.
      key_queue = {}
      return true
    end
    if c == nil or c == "" then return false end
    if c == "\3" then -- Ctrl-C arrived as a literal character
      key_queue = {}
      return true
    end
    key_queue[#key_queue + 1] = c
  end
end

----------------------------------------------------------------------
-- Overlay management
----------------------------------------------------------------------

local function cleanup_overlay()
  if overlay.win and vim.api.nvim_win_is_valid(overlay.win) then
    pcall(vim.api.nvim_win_close, overlay.win, true)
  end
  overlay.win = nil
  overlay.buf = nil
end

local function create_overlay(screen)
  cleanup_overlay()

  overlay.buf = vim.api.nvim_create_buf(false, true)
  vim.bo[overlay.buf].bufhidden = "wipe"
  vim.api.nvim_buf_set_lines(overlay.buf, 0, -1, false, screen.lines)

  local cur_win = vim.api.nvim_get_current_win()
  local win_pos = vim.api.nvim_win_get_position(cur_win)
  overlay.win = vim.api.nvim_open_win(overlay.buf, false, {
    relative = "editor",
    row = win_pos[1],
    col = win_pos[2],
    width = screen.width,
    height = screen.height,
    style = "minimal",
    focusable = false,
    zindex = 50,
  })
  vim.wo[overlay.win].winhighlight = "NormalFloat:Normal"
  vim.wo[overlay.win].wrap = false
end

----------------------------------------------------------------------
-- Animation (synchronous)
--
-- Blocks the main loop. Screen updates via vim.cmd.redraw().
-- Pending input is drained with getcharstr(0) each frame and queued
-- for replay after the animation finishes.
--
-- Returns true if the animation completed, false if aborted (Ctrl-C).
----------------------------------------------------------------------

local function run_animation(before, after)
  -- Find changed lines
  local changed = {}
  for i = 1, math.max(#before.lines, #after.lines) do
    if (before.lines[i] or "") ~= (after.lines[i] or "") then
      changed[#changed + 1] = i
    end
  end
  if #changed == 0 then return true end

  create_overlay(before)

  -- Frame budget: target ~30 fps, batch characters to fill each frame
  local chars_per_sec = BAUD_RATE / 10 -- 8N1
  local target_fps = 30
  local chars_per_frame = math.max(1, math.floor(chars_per_sec / target_fps))
  local frame_ms = math.max(1, math.floor(1000 / target_fps))

  local change_pos = 1
  local col = 0

  while change_pos <= #changed do
    -- Advance one frame's worth of characters
    local budget = chars_per_frame
    while budget > 0 and change_pos <= #changed do
      local line_num = changed[change_pos]
      local target = after.lines[line_num] or ""
      local source = before.lines[line_num] or ""

      col = col + 1
      budget = budget - 1

      if col >= #target then
        pcall(vim.api.nvim_buf_set_lines, overlay.buf, line_num - 1, line_num, false, { target })
        change_pos = change_pos + 1
        col = 0
      else
        local partial = target:sub(1, col) .. source:sub(col + 1)
        pcall(vim.api.nvim_buf_set_lines, overlay.buf, line_num - 1, line_num, false, { partial })
      end
    end

    -- Push frame to terminal, then sleep for the frame interval.
    -- redraw() can throw if Ctrl-C set the interrupt flag.
    local redraw_ok = pcall(vim.cmd.redraw)
    if not redraw_ok then
      cleanup_overlay()
      key_queue = {}
      return false
    end

    vim.uv.sleep(frame_ms)

    -- Drain any keys the user pressed during this frame
    if drain_input() then
      cleanup_overlay()
      return false -- aborted
    end
  end

  cleanup_overlay()
  return true
end

----------------------------------------------------------------------
-- Queue processing
--
-- After an animation finishes, replay queued keys one at a time.
-- Each key is executed, the screen is compared, and if it changed
-- a new animation runs before the next key is replayed.
----------------------------------------------------------------------

local function process_queue()
  -- Disable autocmds during replay so they don't re-enter
  local saved = enabled
  enabled = false

  while #key_queue > 0 do
    local next_key = table.remove(key_queue, 1)

    -- Execute the key through Neovim's normal input processing.
    -- "x" = process typeahead immediately; "t" = treat as typed.
    vim.api.nvim_feedkeys(next_key, "xt", false)
    vim.cmd.redraw()

    local new_screen = capture_screen()
    if screen_changed(prev_screen, new_screen) then
      local ok = run_animation(prev_screen, new_screen)
      prev_screen = capture_screen()
      if not ok then break end -- Ctrl-C aborted
    else
      prev_screen = new_screen
    end
  end

  enabled = saved
end

----------------------------------------------------------------------
-- Change detection (autocmd-driven)
----------------------------------------------------------------------

local function on_screen_change()
  if not enabled or animating then return end

  local ok, current = pcall(capture_screen)
  if not ok then return end

  if prev_screen and screen_changed(prev_screen, current) then
    animating = true

    run_animation(prev_screen, current)
    prev_screen = capture_screen()

    -- Replay any keys that were typed during the animation
    process_queue()

    animating = false
  else
    prev_screen = current
  end
end

----------------------------------------------------------------------
-- Public API
----------------------------------------------------------------------

function M.start()
  if enabled then return end
  enabled = true
  key_queue = {}
  prev_screen = capture_screen()

  augroup = vim.api.nvim_create_augroup("PainfullySlowPOC", { clear = true })
  vim.api.nvim_create_autocmd(
    { "CursorMoved", "CursorMovedI", "TextChanged", "TextChangedI", "WinScrolled" },
    { group = augroup, callback = on_screen_change }
  )
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = augroup,
    once = true,
    callback = function() M.stop() end,
  })

  local cps = BAUD_RATE / 10
  vim.notify(
    string.format("painfully-slow: ON  [%d baud / %d chars per sec]", BAUD_RATE, cps),
    vim.log.levels.INFO
  )
end

function M.stop()
  if not enabled then return end
  enabled = false
  cleanup_overlay()
  animating = false
  key_queue = {}
  prev_screen = nil

  if augroup then
    pcall(vim.api.nvim_del_augroup_by_id, augroup)
    augroup = nil
  end

  vim.notify("painfully-slow: OFF", vim.log.levels.INFO)
end

function M.toggle()
  if enabled then
    M.stop()
  else
    M.start()
  end
end

----------------------------------------------------------------------
-- Commands
----------------------------------------------------------------------

vim.api.nvim_create_user_command("PainfullySlowStart", M.start, {})
vim.api.nvim_create_user_command("PainfullySlowStop", M.stop, {})
vim.api.nvim_create_user_command("PainfullySlowToggle", M.toggle, {})

----------------------------------------------------------------------
-- Global ref for idempotent reload
----------------------------------------------------------------------

_G.__painfully_slow_poc = M

vim.notify("painfully-slow POC loaded. :PainfullySlowStart to begin.", vim.log.levels.INFO)

return M
