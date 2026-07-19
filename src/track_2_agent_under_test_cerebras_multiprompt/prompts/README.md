# Voter system prompts

Each `.md`/`.txt` file in this directory is one system prompt = one voter in
`MultiPromptVotingCARBenchAgentExecutor`. Every benchmark turn, the agent draws
one sample per file in parallel and executes the plurality action.

- Files are loaded sorted by filename; ties in the vote break toward the
  earliest file, so keep the most trusted prompts first.
- Use an odd number of voters to reduce ties.
- `README*` files are ignored; empty files are skipped.
- An alternative prompt set can be selected per run with
  `--prompts-dir <dir>` or `TRACK2_PROMPTS_DIR=<dir>`.
- To cap API load, `--max-prompts <n>` or `TRACK2_MAX_PROMPTS=<n>` loads only
  the first `n` prompt files in sorted filename order.
