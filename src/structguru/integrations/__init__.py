"""Framework integrations for structguru.

Each submodule provides integration with a specific framework. Import the one
you need; this package imports none of them for you, so an extra you have not
installed is never pulled in. Adapters defer their framework import to call
time where they can — ``requests`` is the exception, since its session class
subclasses ``requests.Session`` at module scope.
"""
