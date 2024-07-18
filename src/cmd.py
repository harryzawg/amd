import argparse
import asyncio
import random
import sys
from asyncio import Task

from loguru import logger
from prettytable import PrettyTable
from prompt_toolkit import PromptSession, print_formatted_text, ANSI
from prompt_toolkit.patch_stdout import patch_stdout

from src.adb import Device
from src.api import get_token, init_client_and_lock, get_real_url, get_album_info
from src.config import Config
from src.exceptions import CodecNotFoundException
from src.quality import get_available_song_audio_quality
from src.rip import rip_song, rip_album, rip_artist, rip_playlist
from src.types import GlobalAuthParams
from src.url import AppleMusicURL, URLType, Song
from src.utils import get_song_id_from_m3u8, check_dep


class NewInteractiveShell:
    loop: asyncio.AbstractEventLoop
    config: Config
    tasks: list[Task] = []
    devices: list[Device] = []
    storefront_device_mapping: dict[str, list[Device]] = {}
    anonymous_access_token: str
    parser: argparse.ArgumentParser

    def __init__(self, loop: asyncio.AbstractEventLoop):
        dep_installed, missing_dep = check_dep()
        if not dep_installed:
            logger.error(f"Dependence {missing_dep} was not installed!")
            loop.stop()
            sys.exit()

        self.loop = loop
        self.config = Config.load_from_config()
        init_client_and_lock(self.config.download.proxy, self.config.download.parallelNum)
        self.anonymous_access_token = loop.run_until_complete(get_token())

        self.parser = argparse.ArgumentParser(exit_on_error=False)
        subparser = self.parser.add_subparsers()
        download_parser = subparser.add_parser("download", aliases=["dl"])
        download_parser.add_argument("url", type=str)
        download_parser.add_argument("-c", "--codec",
                                     choices=["alac", "ec3", "aac", "aac-binaural", "aac-downmix", "ac3"],
                                     default="alac")
        download_parser.add_argument("-f", "--force", default=False, action="store_true")
        download_parser.add_argument("--include-participate-songs", default=False, dest="include", action="store_true")
        download_from_file_parser = subparser.add_parser("download-from-file", aliases=["dlf"])
        download_from_file_parser.add_argument("file", type=str)
        download_from_file_parser.add_argument("-f", "--force", default=False, action="store_true")
        download_from_file_parser.add_argument("-c", "--codec",
                                               choices=["alac", "ec3", "aac", "aac-binaural", "aac-downmix", "ac3"],
                                               default="alac")
        m3u8_parser = subparser.add_parser("m3u8")
        m3u8_parser.add_argument("url", type=str)
        m3u8_parser.add_argument("-c", "--codec",
                                 choices=["alac", "ec3", "aac", "aac-binaural", "aac-downmix", "ac3"],
                                 default="alac")
        m3u8_parser.add_argument("-f", "--force", default=False, action="store_true")
        m3u8_parser.add_argument("-q", "--quality", default="", dest="quality")
        quality_parser = subparser.add_parser("quality")
        quality_parser.add_argument("url", type=str)
        subparser.add_parser("exit")

        logger.remove()
        logger.add(lambda msg: print_formatted_text(ANSI(msg), end=""), colorize=True, level="INFO")

        for device_info in self.config.devices:
            device = Device(su_method=device_info.suMethod)
            device.connect(device_info.host, device_info.port)
            logger.info(f"Device {device_info.host}:{device_info.port} has connected")
            self.devices.append(device)
            auth_params = device.get_auth_params()
            if not self.storefront_device_mapping.get(auth_params.storefront.lower()):
                self.storefront_device_mapping.update({auth_params.storefront.lower(): []})
            self.storefront_device_mapping[auth_params.storefront.lower()].append(device)
            if device_info.hyperDecrypt:
                device.hyper_decrypt(list(range(device_info.agentPort, device_info.agentPort + device_info.hyperDecryptNum)))
            else:
                device.start_inject_frida(device_info.agentPort)

    async def command_parser(self, cmd: str):
        if not cmd.strip():
            return
        cmds = cmd.split(" ")
        try:
            args = self.parser.parse_args(cmds)
        except (argparse.ArgumentError, argparse.ArgumentTypeError, SystemExit):
            logger.warning(f"Unknown command: {cmd}")
            return
        match cmds[0]:
            case "download" | "dl":
                await self.do_download(args.url, args.codec, args.force, args.include)
            case "m3u8":
                await self.do_m3u8(args.url, args.codec, args.force)
            case "download-from-file" | "dlf":
                await self.do_download_from_file(args.file, args.codec, args.force)
            case "quality":
                await self.do_quality(args.url)
            case "exit":
                self.loop.stop()
                sys.exit()

    async def do_download(self, raw_url: str, codec: str, force_download: bool, include: bool = False):
        url = AppleMusicURL.parse_url(raw_url)
        if not url:
            real_url = await get_real_url(raw_url)
            url = AppleMusicURL.parse_url(real_url)
            if not url:
                logger.error("Illegal URL!")
                return
        available_device = await self._get_available_device(url.storefront)
        global_auth_param = GlobalAuthParams.from_auth_params_and_token(available_device.get_auth_params(),
                                                                        self.anonymous_access_token)
        match url.type:
            case URLType.Song:
                task = self.loop.create_task(
                    rip_song(url, global_auth_param, codec, self.config, available_device, force_download))
            case URLType.Album:
                task = self.loop.create_task(rip_album(url, global_auth_param, codec, self.config, available_device,
                                                       force_download))
            case URLType.Artist:
                task = self.loop.create_task(rip_artist(url, global_auth_param, codec, self.config, available_device,
                                                        force_download, include))
            case URLType.Playlist:
                task = self.loop.create_task(rip_playlist(url, global_auth_param, codec, self.config, available_device,
                                                          force_download))
            case _:
                logger.error("Unsupported URLType")
                return
        self.tasks.append(task)
        task.add_done_callback(self.tasks.remove)

    async def do_m3u8(self, m3u8_url: str, codec: str, force_download: bool):
        song_id = get_song_id_from_m3u8(m3u8_url)
        song = Song(id=song_id, storefront=self.config.region.defaultStorefront, url="", type=URLType.Song)
        available_device = await self._get_available_device(self.config.region.defaultStorefront)
        global_auth_param = GlobalAuthParams.from_auth_params_and_token(available_device.get_auth_params(),
                                                                        self.anonymous_access_token)
        self.loop.create_task(
            rip_song(song, global_auth_param, codec, self.config, available_device, force_save=force_download,
                     specified_m3u8=m3u8_url)
        )

    async def do_download_from_file(self, file: str, codec: str, force_download: bool):
        with open(file, "r", encoding="utf-8") as f:
            urls = f.readlines()
        for url in urls:
            task = self.loop.create_task(self.do_download(raw_url=url, codec=codec, force_download=force_download))
            self.tasks.append(task)
            task.add_done_callback(self.tasks.remove)

    async def do_quality(self, raw_url: str):
        url = AppleMusicURL.parse_url(raw_url)
        if not url:
            real_url = await get_real_url(raw_url)
            url = AppleMusicURL.parse_url(real_url)
            if not url:
                logger.error("Illegal URL!")
                return
        logger.info(f"Getting data for {url.type} id {url.id}")
        available_device = await self._get_available_device(url.storefront)
        global_auth_param = GlobalAuthParams.from_auth_params_and_token(available_device.get_auth_params(),
                                                                        self.anonymous_access_token)
        match url.type:
            case URLType.Song:
                try:
                    song_metadata, audio_qualities = await get_available_song_audio_quality(url, self.config,
                                                                                            global_auth_param,
                                                                                            available_device)
                except CodecNotFoundException:
                    return
                table = PrettyTable(
                    field_names=["Codec ID", "Codec", "Bitrate", "Average Bitrate", "Channels", "Sample Rate",
                                 "Bit Depth"])
                audio_qualities.sort(key=lambda x: x.bitrate, reverse=True)
                table.add_rows([list(audio_quality.model_dump().values()) for audio_quality in audio_qualities])
                print_formatted_text(
                    f"Available audio qualities for song: {song_metadata.artist} - {song_metadata.title}:")
                print_formatted_text(table)
            case URLType.Album:
                album_info = await get_album_info(url, self.config, global_auth_param)
                songs = album_info.tracks
                for song_metadata, audio_qualities in songs:
                    table = PrettyTable(
                        field_names=["Codec ID", "Codec", "Bitrate", "Average Bitrate", "Channels", "Sample Rate",
                                     "Bit Depth"])
                    audio_qualities.sort(key=lambda x: x.bitrate, reverse=True)
                    table.add_rows([list(audio_quality.model_dump().values()) for audio_quality in audio_qualities])
                    print_formatted_text(f"Available audio qualities for song: {song_metadata.artist} - {song_metadata.title}:")
                    print_formatted_text(table)

    async def _get_available_device(self, storefront: str) -> Device:
        if not self.storefront_device_mapping.get(storefront.lower()):
            storefront = "us"
        devices = self.storefront_device_mapping.get(storefront.lower())
        random.shuffle(devices)
        return devices[0]

    def handle_command_line_args(self):
        if len(sys.argv) > 1:
            cmd = ' '.join(sys.argv[1:])
            self.loop.run_until_complete(self.command_parser(cmd))

    def start(self):
        self.handle_command_line_args()
        session = PromptSession()

        with patch_stdout():
            while True:
                try:
                    text = self.loop.run_until_complete(session.prompt_async("> "))
                    self.loop.run_until_complete(self.command_parser(text))
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    shell = NewInteractiveShell(loop)
    shell.start()