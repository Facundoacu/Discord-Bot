class SourceError(Exception):
    """Error esperable al resolver una fuente de audio.

    El mensaje está pensado para mostrárselo tal cual al usuario en Discord.
    """


class StreamError(Exception):
    """El audio de un tema no se pudo bajar aunque estaba resuelto (p. ej.
    YouTube respondió 403). Es reintentable: con una URL nueva suele andar."""
