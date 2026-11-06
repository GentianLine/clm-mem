"""
Entry point: ``python -m shatterling``.

Equivalent to the installed CLI scripts:
    purslane  serve --use-mock --port 8080
    clm-mem   demo
"""


from .cli import main

if __name__ == "__main__":
    main()
