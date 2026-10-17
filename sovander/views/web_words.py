"""The words Gander's pages in data/assets show, in the app's language (Requirement 14, task 6.4).

The pages are written in English. web_view.py hands them this dictionary, English to the words to
show, before their scripts run and again when the language changes; vwSay() in app.js looks each
word up and keeps the English for any it is not given. The page fills in the {names}.
"""

from sovander.i18n import _, pgettext


def page_words() -> dict[str, str]:
    """English -> the current language, for every word the pages say."""
    return {
        "Book": pgettext("book page", "Book"),
        "Opening book…": _("Opening book…"),
        "Rendering…": _("Rendering…"),
        "Contents": pgettext("book page", "Contents"),
        "Previous": pgettext("book page", "Previous"),
        "Next": pgettext("book page", "Next"),
        "Close": pgettext("book page", "Close"),
        "Remote image not loaded": _("Remote image not loaded"),
        "Could not read the file (HTTP {status})": _("Could not read the file (HTTP {status})"),
        "Something went wrong while rendering": _("Something went wrong while rendering"),
        "Could not render this Markdown file": _("Could not render this Markdown file"),
        "Could not open this book": _("Could not open this book"),
        "It is not an EPUB, or the file is damaged.": _("It is not an EPUB, or the file is damaged."),
        "This book is locked": _("This book is locked"),
        "It is protected against copying (DRM), so only the app of the shop or library it came from can "
        "open it.": _("It is protected against copying (DRM), so only the app of the shop or library it came "
                      "from can open it."),
        "The book does not say where its chapters are listed.":
            _("The book does not say where its chapters are listed."),
        "The book lists no chapters.": _("The book lists no chapters."),
        "The book has no {path}": _("The book has no {path}"),
        "One of the book's own files is not sound XML.": _("One of the book's own files is not sound XML."),
        "Part {n}": _("Part {n}"),
        "This part of the book could not be read.": _("This part of the book could not be read."),
    }
