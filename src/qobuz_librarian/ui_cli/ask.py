"""One way to read an answer from the terminal."""
from qobuz_librarian.ui_cli.colors import C, block, fmt, text_width
from qobuz_librarian.ui_cli.logging import log

NO_ANSWER = (
    "  No answer is available, so nothing was started. Run this again from a "
    "terminal to choose."
)


def _fit(prompt):
    """A prompt wider than the terminal, reflowed; its leading blank lines and
    the space the answer is typed after are kept."""
    if all(len(line) <= text_width() for line in prompt.split("\n")):
        return prompt
    head = prompt[:len(prompt) - len(prompt.lstrip("\n"))]
    body = prompt[len(head):]
    tail = body[len(body.rstrip()):]
    return head + block(body.rstrip()) + tail


def ask(prompt, *, colour=C.CYAN, lower=True, quiet=False):
    """Read one answer from the terminal, or None when nobody is there to give
    one.

    A closed input is not an answer. Reading it as one let a prompt whose
    default is yes start a download in a run nobody was watching, so every
    prompt reads through here and every caller treats None as a cancel. The
    notice is what makes the stop visible in a log nobody was watching either;
    ``quiet`` is for the few prompts that print their own.
    """
    try:
        answer = input(fmt(colour, _fit(prompt)))
    except EOFError:
        if not quiet:
            print()
            log.info(fmt(C.GRAY, NO_ANSWER))
        return None
    answer = answer.strip()
    return answer.lower() if lower else answer
