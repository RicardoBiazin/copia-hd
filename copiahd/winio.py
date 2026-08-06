"""Acesso de baixo nivel a discos fisicos e volumes no Windows via ctypes."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_VOLUME_OFFLINE = 0x0056C00C

FILE_BEGIN = 0


class DISK_GEOMETRY(ctypes.Structure):
    _fields_ = [
        ("Cylinders", ctypes.c_longlong),
        ("MediaType", ctypes.c_ulong),
        ("TracksPerCylinder", ctypes.c_ulong),
        ("SectorsPerTrack", ctypes.c_ulong),
        ("BytesPerSector", ctypes.c_ulong),
    ]


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
]
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
kernel32.GetFileType.argtypes = [wintypes.HANDLE]
kernel32.GetFileType.restype = wintypes.DWORD
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]


class WinIOError(OSError):
    """Erro de I/O do Windows com codigo e contexto legivel."""

    def __init__(self, operacao: str, codigo: int | None = None):
        self.codigo = codigo if codigo is not None else ctypes.get_last_error()
        msg = ctypes.FormatError(self.codigo).strip()
        super().__init__(f"{operacao} falhou (erro {self.codigo}): {msg}")


class DispositivoBruto:
    """Handle para um dispositivo de bloco (\\\\.\\PhysicalDriveN ou \\\\.\\X:).

    Usa I/O sem buffer, portanto todos os offsets e tamanhos precisam ser
    multiplos do tamanho de setor logico.
    """

    def __init__(self, caminho: str, escrita: bool = False, sem_buffer: bool = True):
        self.caminho = caminho
        self.escrita = escrita
        acesso = GENERIC_READ | (GENERIC_WRITE if escrita else 0)
        # I/O sem buffer e obrigatorio em dispositivos brutos, mas em arquivos de
        # imagem ele imporia o tamanho de setor do volume hospedeiro (4096 B em
        # muitos SSDs), quebrando operacoes de 512 B. Em arquivo usamos buffered.
        self.dispositivo = caminho.startswith("\\\\.\\")
        flags = FILE_FLAG_NO_BUFFERING if (sem_buffer and self.dispositivo) else 0
        if escrita:
            flags |= FILE_FLAG_WRITE_THROUGH
        handle = kernel32.CreateFileW(
            caminho,
            acesso,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            raise WinIOError(f"Abrir {caminho}")
        self.handle = handle
        self._fechado = False

    # -- consultas -------------------------------------------------------
    def tamanho_setor(self) -> int:
        geo = DISK_GEOMETRY()
        devolvido = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            self.handle,
            IOCTL_DISK_GET_DRIVE_GEOMETRY,
            None,
            0,
            ctypes.byref(geo),
            ctypes.sizeof(geo),
            ctypes.byref(devolvido),
            None,
        )
        if not ok:
            # arquivos de imagem nao respondem a IOCTL de disco
            if self._eh_arquivo():
                return 512
            raise WinIOError("IOCTL_DISK_GET_DRIVE_GEOMETRY")
        return int(geo.BytesPerSector)

    def tamanho_bytes(self) -> int:
        tamanho = ctypes.c_longlong(0)
        devolvido = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            self.handle,
            IOCTL_DISK_GET_LENGTH_INFO,
            None,
            0,
            ctypes.byref(tamanho),
            ctypes.sizeof(tamanho),
            ctypes.byref(devolvido),
            None,
        )
        if not ok:
            if self._eh_arquivo():
                if not kernel32.GetFileSizeEx(self.handle, ctypes.byref(tamanho)):
                    raise WinIOError("GetFileSizeEx")
                return int(tamanho.value)
            raise WinIOError("IOCTL_DISK_GET_LENGTH_INFO")
        return int(tamanho.value)

    def _eh_arquivo(self) -> bool:
        """True para arquivos comuns (imagens .img usadas nos testes)."""
        return not self.dispositivo

    # -- posicionamento e transferencia ----------------------------------
    def posicionar(self, offset: int) -> None:
        nova = ctypes.c_longlong(0)
        if not kernel32.SetFilePointerEx(
            self.handle, ctypes.c_longlong(offset), ctypes.byref(nova), FILE_BEGIN
        ):
            raise WinIOError(f"SetFilePointerEx({offset})")

    def ler_em(self, buffer, tamanho: int) -> int:
        lidos = wintypes.DWORD(0)
        if not kernel32.ReadFile(
            self.handle, buffer, tamanho, ctypes.byref(lidos), None
        ):
            raise WinIOError("ReadFile")
        return int(lidos.value)

    def escrever_de(self, buffer, tamanho: int) -> int:
        escritos = wintypes.DWORD(0)
        if not kernel32.WriteFile(
            self.handle, buffer, tamanho, ctypes.byref(escritos), None
        ):
            raise WinIOError("WriteFile")
        return int(escritos.value)

    def ler(self, offset: int, tamanho: int) -> bytes:
        buf = buffer_alinhado(tamanho)
        self.posicionar(offset)
        lidos = self.ler_em(buf, tamanho)
        return bytes(bytearray(buf)[:lidos])

    def escrever(self, offset: int, dados: bytes) -> int:
        buf = buffer_alinhado(len(dados))
        ctypes.memmove(buf, dados, len(dados))
        self.posicionar(offset)
        return self.escrever_de(buf, len(dados))

    # -- controle de volume ----------------------------------------------
    def _ioctl_simples(self, codigo: int) -> bool:
        devolvido = wintypes.DWORD()
        return bool(
            kernel32.DeviceIoControl(
                self.handle, codigo, None, 0, None, 0, ctypes.byref(devolvido), None
            )
        )

    def travar_volume(self) -> bool:
        return self._ioctl_simples(FSCTL_LOCK_VOLUME)

    def destravar_volume(self) -> bool:
        return self._ioctl_simples(FSCTL_UNLOCK_VOLUME)

    def desmontar_volume(self) -> bool:
        return self._ioctl_simples(FSCTL_DISMOUNT_VOLUME)

    def volume_offline(self) -> bool:
        return self._ioctl_simples(IOCTL_VOLUME_OFFLINE)

    def atualizar_propriedades(self) -> bool:
        """Faz o Windows reler a tabela de particoes do disco."""
        return self._ioctl_simples(IOCTL_DISK_UPDATE_PROPERTIES)

    # -- ciclo de vida ---------------------------------------------------
    def fechar(self) -> None:
        if not self._fechado and self.handle:
            kernel32.CloseHandle(self.handle)
            self._fechado = True

    def __enter__(self) -> "DispositivoBruto":
        return self

    def __exit__(self, *_exc) -> None:
        self.fechar()


def buffer_alinhado(tamanho: int, alinhamento: int = 4096):
    """Buffer alinhado ao tamanho de pagina, exigido por FILE_FLAG_NO_BUFFERING."""
    bruto = ctypes.create_string_buffer(tamanho + alinhamento)
    endereco = ctypes.addressof(bruto)
    deslocamento = (alinhamento - (endereco % alinhamento)) % alinhamento
    vista = (ctypes.c_char * tamanho).from_buffer(bruto, deslocamento)
    vista._ancora = bruto  # evita coleta do buffer subjacente
    return vista


def caminho_disco(numero: int) -> str:
    return rf"\\.\PhysicalDrive{numero}"


def caminho_volume(letra: str) -> str:
    return rf"\\.\{letra.rstrip(':').upper()}:"
