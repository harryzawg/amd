import asyncio
import sys

from src.cmd import NewInteractiveShell

if __name__ == '__main__':
    if sys.platform in ('win32', 'cygwin', 'cli'):
        import winloop
        winloop.install()
        print("using uvloop")
    else:
        import uvloop
        uvloop.install()
        print("using asyncio")
    loop = asyncio.get_event_loop()
    cmd = NewInteractiveShell(loop)
    try:
        loop.run_until_complete(cmd.start())
    except KeyboardInterrupt:
        loop.stop()
