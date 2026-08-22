"""Radio station target type.

Isolated here on purpose: everything under ``discovery.radio`` knows about
radio; everything above it does not know about radio. Future org types add
sibling packages reusing crawler/, enrichment/ and discovery core unchanged.
"""
