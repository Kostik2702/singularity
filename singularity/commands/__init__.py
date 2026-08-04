"""`mb` command implementations, grouped by what they are about.

`cli.py` keeps the parser and the dispatcher; each module here holds the
handlers for one area, so a 1500-line entry point stopped being the place
where every feature of the engine meets.
"""
