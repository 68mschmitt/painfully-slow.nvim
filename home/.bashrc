# Apple IIe remote shell environment
PS1='$ '
export TERM="${TERM:-vt100}"
export LANG="${LANG:-en_US.UTF-8}"

# Use a separate tmux server socket so we don't connect to the user's
# existing server (which loaded their real ~/.tmux.conf at startup).
alias tmux='tmux -L a2term'
