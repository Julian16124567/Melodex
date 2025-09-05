import argparse
import logging
import os
import re
import sys
import time
from typing import List, Dict, Optional

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from yt_dlp import YoutubeDL
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC

# Configuration constants
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
DEFAULT_OUTPUT_DIR = "downloads"
DEFAULT_QUALITY = "high"
MAX_RETRIES = 3
RETRY_DELAY = 5

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-read-private playlist-read-collaborative"
        ))

    def get_playlist_tracks(self, playlist_url: str) -> List[Dict]:
        playlist_id = self._extract_playlist_id(playlist_url)
        tracks = []
        try:
            results = self.sp.playlist_tracks(playlist_id)
            tracks.extend(results['items'])
            while results['next']:
                results = self.sp.next(results)
                tracks.extend(results['items'])
        except spotipy.SpotifyException as e:
            logging.error(f"Spotify API error: {e}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Error fetching playlist: {e}")
            sys.exit(1)
        return [self._extract_metadata(item['track']) for item in tracks if item.get('track')]

    def _extract_playlist_id(self, url: str) -> str:
        match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        if not match:
            logging.error("Invalid Spotify playlist URL.")
            sys.exit(1)
        return match.group(1)

    def _extract_metadata(self, track: Dict) -> Dict:
        artists = ", ".join([artist['name'] for artist in track['artists']])
        album = track['album']['name']
        title = track['name']
        cover_url = track['album']['images'][0]['url'] if track['album']['images'] else None
        return {
            "title": title,
            "artists": artists,
            "album": album,
            "cover_url": cover_url
        }


class YouTubeDownloader:
    def __init__(self, quality: str = DEFAULT_QUALITY):
        self.quality = quality
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': '%(title)s.%(ext)s',
            'quiet': True,
            'noplaylist': True,
            'progress_hooks': [self._progress_hook],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192' if quality == "high" else "128",
            }]
        }

    def download(self, url: str, output_path: str) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                with YoutubeDL(self.ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    mp3_file = os.path.splitext(filename)[0] + ".mp3"
                    if os.path.exists(mp3_file):
                        os.rename(mp3_file, output_path)
                        return output_path
                    else:
                        logging.error("Downloaded file not found.")
                        return None
            except Exception as e:
                logging.warning(f"Download failed (attempt {attempt+1}): {e}")
                time.sleep(RETRY_DELAY)
        logging.error("Failed to download after multiple attempts.")
        return None

    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            logging.info(f"Downloading: {d.get('filename', '')} - {d.get('_percent_str', '')}")
        elif d['status'] == 'finished':
            logging.info("Download finished.")


class SongMatcher:
    def __init__(self):
        pass

    def search_youtube(self, metadata: Dict) -> Optional[str]:
        query = f"{metadata['artists']} - {metadata['title']} audio"
        search_url = f"ytsearch5:{query}"
        try:
            with YoutubeDL({'quiet': True}) as ydl:
                results = ydl.extract_info(search_url, download=False)['entries']
                best_match = self._find_best_match(results, metadata)
                if best_match:
                    return f"https://www.youtube.com/watch?v={best_match['id']}"
        except Exception as e:
            logging.warning(f"YouTube search failed: {e}")
        return None

    def _find_best_match(self, results: List[Dict], metadata: Dict) -> Optional[Dict]:
        for entry in results:
            title = entry.get('title', '').lower()
            if self._is_match(title, metadata):
                return entry
        # Fallback: return first result
        return results[0] if results else None

    def _is_match(self, yt_title: str, metadata: Dict) -> bool:
        song_title = metadata['title'].lower()
        artist = metadata['artists'].split(",")[0].lower()
        return artist in yt_title and song_title in yt_title


class MetadataHandler:
    def __init__(self):
        pass

    def embed_metadata(self, file_path: str, metadata: Dict):
        try:
            audio = EasyID3(file_path)
        except Exception:
            audio = EasyID3()
        audio['title'] = metadata['title']
        audio['artist'] = metadata['artists']
        audio['album'] = metadata['album']
        audio.save(file_path)
        if metadata.get('cover_url'):
            self._embed_cover(file_path, metadata['cover_url'])

    def _embed_cover(self, file_path: str, cover_url: str):
        try:
            response = requests.get(cover_url, timeout=10)
            response.raise_for_status()
            img_data = response.content
            audio = ID3(file_path)
            audio['APIC'] = APIC(
                encoding=3,
                mime='image/jpeg',
                type=3,
                desc='Cover',
                data=img_data
            )
            audio.save(file_path)
        except Exception as e:
            logging.warning(f"Failed to embed cover art: {e}")

    def get_output_path(self, metadata: Dict, output_dir: str) -> str:
        safe_artist = self._sanitize(metadata['artists'].split(",")[0])
        safe_album = self._sanitize(metadata['album'])
        safe_title = self._sanitize(metadata['title'])
        dir_path = os.path.join(output_dir, safe_artist, safe_album)
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, f"{safe_title}.mp3")

    def _sanitize(self, name: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "", name)


def main():
    parser = argparse.ArgumentParser(description="Spotify Playlist Downloader for Terminal Music Player")
    parser.add_argument("--playlist", type=str, help="Spotify playlist URL")
    parser.add_argument("--user-playlists", action="store_true", help="Download all user playlists")
    parser.add_argument("--quality", type=str, choices=["high", "low"], default=DEFAULT_QUALITY, help="Audio quality")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        logging.error("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.")
        sys.exit(1)

    spotify = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI)
    yt_downloader = YouTubeDownloader(args.quality)
    matcher = SongMatcher()
    metadata_handler = MetadataHandler()

    playlists = []
    if args.playlist:
        playlists = [args.playlist]
    elif args.user_playlists:
        try:
            user_id = spotify.sp.current_user()['id']
            results = spotify.sp.user_playlists(user_id)
            playlists = [pl['external_urls']['spotify'] for pl in results['items']]
        except Exception as e:
            logging.error(f"Failed to fetch user playlists: {e}")
            sys.exit(1)
    else:
        logging.error("Provide --playlist URL or --user-playlists.")
        sys.exit(1)

    for playlist_url in playlists:
        logging.info(f"Processing playlist: {playlist_url}")
        tracks = spotify.get_playlist_tracks(playlist_url)
        for idx, metadata in enumerate(tracks, 1):
            logging.info(f"[{idx}/{len(tracks)}] {metadata['artists']} - {metadata['title']}")
            output_path = metadata_handler.get_output_path(metadata, args.output)
            if os.path.exists(output_path):
                logging.info("Track already downloaded. Skipping.")
                continue
            yt_url = matcher.search_youtube(metadata)
            if not yt_url:
                logging.warning("No matching YouTube video found. Skipping.")
                continue
            temp_path = output_path + ".tmp"
            downloaded = yt_downloader.download(yt_url, temp_path)
            if downloaded:
                os.rename(temp_path, output_path)
                metadata_handler.embed_metadata(output_path, metadata)
                logging.info(f"Downloaded and tagged: {output_path}")
            else:
                logging.warning("Download failed. Skipping.")

if __name__ == "__main__":
    main()
