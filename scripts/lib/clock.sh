# shellcheck shell=bash
# ---------------------------------------------------------------------------
# clock.sh — sourced, not executed; defines functions only.
#
# format_duration <total_seconds> -> "42s", "9m 57s", "1h 04m 12s"
#
# Reports in units that match the number, so nobody has to divide 597 by 60 to
# learn that a run took ten minutes.
# ---------------------------------------------------------------------------

format_duration() {
  local total_seconds="$1"
  local hours=$(( total_seconds / 3600 ))
  local minutes=$(( (total_seconds % 3600) / 60 ))
  local seconds=$(( total_seconds % 60 ))

  if (( hours > 0 )); then
    printf '%dh %02dm %02ds' "$hours" "$minutes" "$seconds"
  elif (( minutes > 0 )); then
    printf '%dm %02ds' "$minutes" "$seconds"
  else
    printf '%ds' "$seconds"
  fi
}
