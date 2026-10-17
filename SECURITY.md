# Security

Sovander exists to open files from strangers safely, so reports about how a file could harm a computer
through it are the most welcome reports of all.

## Reporting a problem

Please report privately, not in a public issue:

- **GitHub:** [Report a vulnerability](https://github.com/khdrvss/Sovander/security/advisories/new)
  (private to the maintainer), or
- **E-mail:** Saidxon Xaydarov <saidxon3105@gmail.com>, subject starting with `[sovander security]`.

Include what you opened (or how to make the file), what happened, and the Ubuntu release and Sovander
version (menu → About Sovander). A sample file helps most; if it is dangerous, say so
and it will be handled as such.

You will get an answer within a week. Once a fix is released, the advisory credits you unless you would
rather not be named.

## What counts

- A file that makes Sovander or its engine run code, read other files, write anywhere, or reach the network.
- A way around the AppArmor profile, or the start-up check saying "protected" when it is not.
- A file that freezes the computer or exhausts its memory instead of failing with a message.
- A link or remote resource that opens or loads without the confirmation Sovander promises.

Bugs in LibreOffice, MuPDF, WebKitGTK or GTK themselves belong upstream; tell us too if Sovander's use of
them makes the problem reachable.

## Supported versions

The latest release. Sovander follows Ubuntu's packages for its engine and libraries, so keep Ubuntu
updated as well.
