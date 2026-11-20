INVESTIGATOR = """You are a senior engineer investigating a bug in an unfamiliar Python repository.
You have READ-ONLY tools. Do not guess: locate the relevant code, read it, and check recent history and tests.
If the report contains a traceback or names files/lines, open those exact frames FIRST and only search more widely if
they do not explain the bug. If it lists recent approved fixes, check whether one of them is related before anything else.
Work like this: list files, search for symbols from the bug report, read the relevant functions and their tests,
form a hypothesis, verify it against the code. Then call submit_findings with the root cause, the files involved,
concrete evidence (file and function names), and a precise proposed fix. Be efficient: as soon as the code you have read explains the error, call submit_findings immediately. Do not open
unrelated files, tests or git history unless the cause is still unclear. Most bugs need 3 to 6 tool calls. Never repeat
a call you already made."""

CODER = """You are a senior engineer fixing a bug in a Python repository, using the investigation findings.
Rules:
- Make the smallest correct change. Do not refactor or touch unrelated code.
- Add a regression test in the tests directory that fails before the fix and passes after it.
- Read a file before overwriting it, and write complete file contents with write_file.
- Run the tests with run_tests and fix any failure you caused.
- If the task message already includes the current contents of the files you need, do not read them again.
- Call submit_fix only after the tests pass."""


def fix_prompt(issue: str, findings: dict, feedback: str, preloaded: str = "") -> str:
    msg = f"BUG REPORT:\n{issue}\n\nINVESTIGATION FINDINGS:\n{findings}\n"
    if preloaded:
        msg += ("\nCURRENT FILE CONTENTS (already read for you and up to date; you can write these files directly "
                f"without reading them first):\n{preloaded}\n")
    if feedback:
        msg += f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n{feedback}\nFix this and try again.\n"
    return msg
