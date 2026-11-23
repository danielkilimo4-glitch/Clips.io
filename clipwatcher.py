
import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from array import array
from bisect import bisect_left, insort
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any

SCRIPT_DIR = Path(__file__).resolve().parent
SEG_SECONDS = 10                                                                                    # length of each buffer segment
SEG_RE = re.compile(r"seg_(\d{8}-\d{6})\.ts$")
STOP = threading.Event()

# =============================================================================
# AI ANALYZERS - Intelligent highlight detection
# =============================================================================

@dataclass
class AIResult:
    """Result from an AI analyzer."""
    analyzer: str
    score: float  # 0-10
    details: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class AIAnalyzerBase:
    """Base class for AI analyzers."""
    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self.model = None

    def warmup(self):
        """Load models. Override in subclasses."""
        pass

    def analyze(self, segment_path: Path, context: dict) -> AIResult:
        """Analyze a segment. Override in subclasses."""
        if not self.enabled:
            return AIResult(analyzer=self.name, score=0.0, details={"disabled": True})
        return AIResult(analyzer=self.name, score=0.0, details={"not_implemented": True})


class WhisperAudioAnalyzer(AIAnalyzerBase):
    """Audio analysis using Whisper: transcription + hype keywords + laughter + energy."""

    HYPE_KEYWORDS = [
        "pog", "poggers", "pogchamp", "let's go", "lets go", "insane", "crazy",
        "unbelievable", "no way", "omg", "wtf", "holy", "clutch", "godlike",
        "cracked", "nutty", "disgusting", "filthy", "sick", "nasty", "monster",
        "beast", "goat", "carry", "throwing", "choke", "ez", "free", "gifted"
    ]

    def __init__(self, model_name: str = "base", device: str = "auto", enabled: bool = True):
        super().__init__("audio_ai", enabled)
        self.model_name = model_name
        self.device = device
        self._whisper = None

    def warmup(self):
        if not self.enabled:
            return
        try:
            import whisper
            import torch
            if self.device == "auto":
                if torch.cuda.is_available():
                    self.device = "cuda"
                elif torch.backends.mps.is_available():
                    self.device = "mps"
                else:
                    self.device = "cpu"
            log(f"  Loading Whisper {self.model_name} on {self.device}...")
            self._whisper = whisper.load_model(self.model_name, device=self.device)
            log(f"  Whisper ready")
        except ImportError:
            log("  Whisper not installed, audio AI disabled")
            self.enabled = False
        except Exception as e:
            log(f"  Whisper load failed: {e}")
            self.enabled = False

    def analyze(self, segment_path: Path, context: dict) -> AIResult:
        if not self.enabled or not self._whisper:
            return AIResult(analyzer="audio_ai", score=0.0, details={"error": "not_loaded"})

        start = time.time()
        details = {}

        try:
            import whisper
            import numpy as np

            # Load audio
            audio = whisper.load_audio(str(segment_path))
            audio = whisper.pad_or_trim(audio)

            # Energy analysis
            energy = float(np.sqrt(np.mean(audio**2)))
            details["energy"] = energy

            # Transcribe
            result = self._whisper.transcribe(audio, fp16=self.device != "cpu")
            text = result.get("text", "").strip().lower()
            details["transcript"] = text[:200]  # truncate for logging
            details["language"] = result.get("language", "unknown")

            # Keyword detection
            matches = [kw for kw in self.HYPE_KEYWORDS if kw in text]
            details["hype_keywords"] = matches
            keyword_score = min(len(matches) * 1.5, 10.0)

            # Laughter detection
            laughter_patterns = [
                r"\b(ha\s*){3,}\b", r"\b(lol|lmao|rofl|lul)\b", r"\b(xd+)\b",
                r"[😂🤣😆😭]"
            ]
            laughter_count = sum(len(re.findall(p, text)) for p in laughter_patterns)
            details["laughter_count"] = laughter_count
            laughter_score = min(laughter_count * 2.0, 10.0)

            # Energy score (rough scaling)
            energy_score = min(energy * 100, 10.0)

            # Combined
            score = (keyword_score * 0.4 + energy_score * 0.3 + laughter_score * 0.3)
            details.update({
                "keyword_score": keyword_score,
                "energy_score": energy_score,
                "laughter_score": laughter_score,
            })

            return AIResult(
                analyzer="audio_ai",
                score=min(score, 10.0),
                details=details,
                timestamp=time.time()
            )

        except Exception as e:
            return AIResult(
                analyzer="audio_ai",
                score=0.0,
                details={"error": str(e)},
                timestamp=time.time()
            )


class VisionAnalyzer(AIAnalyzerBase):
    """Visual analysis using vision LLM (OpenAI GPT-4V or compatible)."""

    def __init__(self, model: str = "gpt-4o-mini", api_key: str = "", enabled: bool = True, sample_interval: int = 5):
        super().__init__("visual_ai", enabled)
        self.model = model
        self.api_key = api_key
        self.sample_interval = sample_interval
        self._client = None

    def warmup(self):
        if not self.enabled or not self.api_key:
            if self.enabled:
                log("  Vision AI: no API key provided, disabled")
            self.enabled = False
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key)
            log(f"  Vision AI ready: {self.model}")
        except ImportError:
            log("  OpenAI package not installed, vision AI disabled")
            self.enabled = False
        except Exception as e:
            log(f"  Vision AI init failed: {e}")
            self.enabled = False

    def analyze(self, segment_path: Path, context: dict) -> AIResult:
        if not self.enabled or not self._client:
            return AIResult(analyzer="visual_ai", score=0.0, details={"error": "not_loaded"})

        start = time.time()
        details = {}

        try:
            # Extract middle frame
            frame_path = self._extract_frame(segment_path)
            if not frame_path:
                return AIResult(analyzer="visual_ai", score=0.0, details={"error": "frame_extract_failed"})

            # Encode image
            import base64
            with open(frame_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()

            # Analyze with vision model
            prompt = (
                "Rate 0-10: Is this frame a highlight moment from a live stream? Look for:\n"
                "- Big plays, wins, kills, clutch moments in games\n"
                "- Funny reactions, surprised/excited faces\n"
                "- Visual gags, glitches, Easter eggs\n"
                "- Achievement unlocks, level ups, victory screens\n"
                "- Streamer celebrating or reacting strongly\n"
                "Return only a number 0-10."
            )

            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                    ]
                }],
                max_tokens=10,
                temperature=0
            )

            text = response.choices[0].message.content.strip()
            try:
                score = float(text)
                score = max(0, min(10, score))
            except ValueError:
                import re as _re
                match = _re.search(r'(\d+(?:\.\d+)?)', text)
                score = float(match.group(1)) if match else 0.0

            details["frame_path"] = str(frame_path)
            details["raw_response"] = text

            # Clean up frame
            try:
                frame_path.unlink()
            except:
                pass

            return AIResult(
                analyzer="visual_ai",
                score=score,
                details=details,
                timestamp=time.time()
            )

        except Exception as e:
            return AIResult(
                analyzer="visual_ai",
                score=0.0,
                details={"error": str(e)},
                timestamp=time.time()
            )

    def _extract_frame(self, video_path: Path) -> Optional[Path]:
        """Extract a frame from the middle of the video segment."""
        import subprocess
        output_path = video_path.with_suffix(".jpg")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", "00:00:05",
            "-vframes", "1",
            "-q:v", "2",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        return None


class ChatSentimentAnalyzer(AIAnalyzerBase):
    """Chat analysis: velocity, sentiment, emote density, keywords."""

    def __init__(self, enabled: bool = True, model_name: str = "cardiffnlp/twitter-roberta-base-sentiment"):
        super().__init__("chat_ai", enabled)
        self.model_name = model_name
        self._pipeline = None
        self._message_buffer: List[dict] = []

    def warmup(self):
        if not self.enabled:
            return
        try:
            from transformers import pipeline
            self._pipeline = pipeline(
                "sentiment-analysis",
                model=self.model_name,
                tokenizer=self.model_name,
                device=-1
            )
            log(f"  Chat sentiment model loaded: {self.model_name}")
        except ImportError:
            log("  Transformers not installed, chat sentiment disabled")
            self.enabled = False
        except Exception as e:
            log(f"  Chat sentiment load failed: {e}")
            self.enabled = False

    def add_message(self, timestamp: float, text: str, user: str = ""):
        """Add a chat message to the buffer."""
        self._message_buffer.append({
            "timestamp": timestamp,
            "text": text,
            "user": user
        })
        # Keep last 5 minutes
        cutoff = time.time() - 300
        self._message_buffer = [m for m in self._message_buffer if m["timestamp"] > cutoff]

    def analyze(self, segment_path: Path, context: dict) -> AIResult:
        if not self.enabled:
            return AIResult(analyzer="chat_ai", score=0.0, details={"disabled": True})

        start = time.time()
        details = {}

        try:
            segment_start = context.get("segment_start", time.time() - 10)
            segment_end = context.get("segment_end", time.time())

            # Filter messages in window
            window_messages = [
                m for m in self._message_buffer
                if segment_start <= m["timestamp"] <= segment_end
            ]

            if not window_messages:
                return AIResult(
                    analyzer="chat_ai",
                    score=0.0,
                    details={"message_count": 0},
                    timestamp=time.time()
                )

            # Velocity
            window_duration = segment_end - segment_start
            velocity = len(window_messages) / max(window_duration, 1)
            details["velocity"] = velocity
            details["message_count"] = len(window_messages)
            velocity_score = min(velocity / 50.0 * 10, 10.0)  # 50 msgs/sec = max

            # Sentiment
            sentiment_scores = {"positive": 0, "negative": 0, "neutral": 0}
            if self._pipeline:
                texts = [m.get("text", "") for m in window_messages if m.get("text")]
                if texts:
                    results = self._pipeline(texts[:50])
                    for r in results:
                        label = r["label"].lower()
                        if "pos" in label:
                            sentiment_scores["positive"] += 1
                        elif "neg" in label:
                            sentiment_scores["negative"] += 1
                        else:
                            sentiment_scores["neutral"] += 1

            details["sentiment"] = sentiment_scores
            total = sum(sentiment_scores.values())
            sentiment_score = 0.0
            if total > 0:
                sentiment_score = (sentiment_scores["positive"] / total) * 10

            # Emote density (uppercase words)
            emote_count = 0
            for m in window_messages:
                text = m.get("text", "")
                words = text.split()
                emote_count += sum(1 for w in words if w.isupper() and len(w) > 2)
            details["emote_count"] = emote_count
            emote_score = min(emote_count / max(len(window_messages), 1) * 20, 10.0)

            # Keywords
            keywords = ["clip it", "clippable", "highlight", "timestamp", "pog", "poggers"]
            keyword_matches = sum(
                1 for m in window_messages
                if any(kw in m.get("text", "").lower() for kw in keywords)
            )
            details["keyword_matches"] = keyword_matches
            keyword_score = min(keyword_matches * 2.0, 10.0)

            # Combined
            score = (velocity_score * 0.4 + sentiment_score * 0.2 +
                    emote_score * 0.2 + keyword_score * 0.2)
            details.update({
                "velocity_score": velocity_score,
                "sentiment_score": sentiment_score,
                "emote_score": emote_score,
                "keyword_score": keyword_score,
            })

            return AIResult(
                analyzer="chat_ai",
                score=min(score, 10.0),
                details=details,
                timestamp=time.time()
            )

        except Exception as e:
            return AIResult(
                analyzer="chat_ai",
                score=0.0,
                details={"error": str(e)},
                timestamp=time.time()
            )


class FusionScorer:
    """Combines multiple AI analyzer outputs into a single highlight score."""

    def __init__(self, threshold: float = 6.5, cooldown_seconds: int = 60):
        self.weights = {
            "audio_ai": 0.30,      # Whisper transcription + hype + laughter
            "visual_ai": 0.30,     # Vision model frame analysis
            "chat_ai": 0.25,       # Chat velocity + sentiment + emotes
            "audio_loudness": 0.10, # Existing RMS loudness
            "chat_spike": 0.05,    # Existing statistical spike
        }
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._last_clip_time: Dict[str, float] = {}
        self._clip_count = 0
        self._feedback_buffer: List[dict] = []

    def score(self, ai_results: List[AIResult], audio_loudness: float = 0,
              chat_spike_ratio: float = 0, channel_key: str = "") -> tuple:
        """Calculate fused highlight score. Returns (total_score, breakdown, should_clip)."""

        # Extract AI scores
        ai_scores = {r.analyzer: r.score for r in ai_results}

        # Normalize audio loudness to 0-10 scale (0 dB above normal = 0, 20 dB = 10)
        audio_loudness_norm = min(max(audio_loudness, 0) / 20.0 * 10, 10.0)

        # Normalize chat spike ratio to 0-10 (1x = 0, 5x = 10)
        chat_spike_norm = min(max(chat_spike_ratio - 1, 0) / 4.0 * 10, 10.0)

        # Weighted fusion
        breakdown = {}
        total = 0.0

        for key, weight in self.weights.items():
            if key == "audio_loudness":
                val = audio_loudness_norm
            elif key == "chat_spike":
                val = chat_spike_norm
            else:
                val = ai_scores.get(key, 0.0)

            weighted = val * weight
            breakdown[key] = weighted
            total += weighted

        # Check cooldown
        on_cooldown = False
        if channel_key:
            last = self._last_clip_time.get(channel_key, 0)
            on_cooldown = (time.time() - last) < self.cooldown_seconds

        should_clip = total >= self.threshold and not on_cooldown

        if should_clip and channel_key:
            self._last_clip_time[channel_key] = time.time()
            self._clip_count += 1

        return total, breakdown, should_clip

    def record_feedback(self, channel_key: str, score: float, breakdown: dict, approved: bool):
        """Record user feedback for learning."""
        self._feedback_buffer.append({
            "timestamp": time.time(),
            "channel": channel_key,
            "score": score,
            "breakdown": breakdown,
            "approved": approved
        })
        # Keep last 1000
        if len(self._feedback_buffer) > 1000:
            self._feedback_buffer = self._feedback_buffer[-1000:]

    def get_stats(self) -> dict:
        return {
            "total_clips": self._clip_count,
            "weights": self.weights,
            "threshold": self.threshold,
            "feedback_count": len(self._feedback_buffer),
        }


class SlidingWindowMedian:
    """Sliding window median using a sorted list. O(log n) insert, O(n) remove, O(1) median.
    For window sizes ~600 (5 min at 0.5s intervals), O(n) remove is very fast."""
    def __init__(self, window_seconds, resolution, min_readings=2):
        self.window_seconds = window_seconds
        self.resolution = resolution
        self.min_readings = min_readings
        self.data = []          # list of (value, timestamp), kept sorted by value
        self.by_time = []       # list of (timestamp, value), kept sorted by timestamp

    def add(self, timestamp, value):
        """Add a new reading. Removes expired entries."""
        # Remove expired entries (older than window_seconds)
        cutoff = timestamp - self.window_seconds
        while self.by_time and self.by_time[0][0] < cutoff:
            old_ts, old_val = self.by_time.pop(0)
            # Remove from sorted data list
            idx = bisect_left(self.data, (old_val, old_ts))
            # Find exact match (there could be duplicate values)
            while idx < len(self.data) and self.data[idx][0] == old_val:
                if self.data[idx][1] == old_ts:
                    self.data.pop(idx)
                    break
                idx += 1
        # Insert new entry (keep both lists sorted)
        insort(self.by_time, (timestamp, value))
        insort(self.data, (value, timestamp))

    def median(self):
        """Return median of current window, or None if insufficient data."""
        n = len(self.data)
        if n < self.min_readings:
            return None
        mid = n // 2
        if n % 2 == 0:
            return (self.data[mid - 1][0] + self.data[mid][0]) / 2.0
        return self.data[mid][0]

    def __len__(self):
        return len(self.data)


# Need bisect for SlidingWindowMedian
from bisect import bisect_left, insort


def log(msg)
