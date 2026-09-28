'''Replay timing test; no microphone or Holyrics requests.'''
import argparse
import cProfile
import hashlib
import io
import json
import pstats
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/bible_parser_core/src'))
from bible_parser_core.live_pipeline import LiveReferencePipeline
from bible_parser_core.sherpa_streaming import load_sherpa_recognizer, SherpaStreamingRecognizer
from bible_parser_core.version import __version__

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--threads', type=int, default=1)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--regex-cache', type=int, default=0, help='Diagnostic-only Python regex cache size; 0 keeps default.')
    p.add_argument('--skip-audio', action='store_true')
    a = p.parse_args()
    if a.output.exists() or a.repeats < 1:
        p.error('Use a new output file and positive repeat count.')
    if a.regex_cache:
        re._MAXCACHE = a.regex_cache
        re._MAXCACHE2 = a.regex_cache // 2
        re.purge()
    events = a.session / 'events.jsonl'
    phrases = [r for r in map(json.loads, events.read_text(encoding='utf-8').splitlines()) if r.get('event') == 'final_raw' and r.get('text', '').strip()]
    if not phrases:
        p.error('No final phrases in session.')
    report = dict(version=__version__, python=sys.version, command=sys.argv, session=str(a.session), events_sha256=digest(events), commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    report['limitations'] = 'Offline isolated stages, not end-to-end live latency; live engine remains running. Synthetic parser wall times are 10 seconds apart; original ASR word times are preserved. Profiling timings include profiler overhead.'
    report['regex_cache_size'] = re._MAXCACHE
    report['parser_sha256'] = digest(ROOT / 'packages/bible_parser_core/src/bible_parser_core/parser.py')
    report['pipeline_sha256'] = digest(ROOT / 'packages/bible_parser_core/src/bible_parser_core/live_pipeline.py')
    report['bible_sha256'] = digest(ROOT / 'packages/bible_parser_core/src/bible_parser_core/data/rst.json')
    timings = []
    for repeat in range(a.repeats):
        pipeline = LiveReferencePipeline()
        for i, r in enumerate(phrases):
            start = time.perf_counter()
            result = pipeline.process_text(r['text'], asr_result=r.get('result'), now_ms=i * 10000)
            timings.append(dict(repeat=repeat, text=r['text'], ms=1000*(time.perf_counter()-start), reference=(result.get('parsed') or {}).get('ref')))
        print('Parser repeat', repeat+1, 'complete', flush=True)
    report['parser'] = timings
    profiler = cProfile.Profile()
    pipeline = LiveReferencePipeline()
    profiler.enable()
    for i, r in enumerate(phrases):
        pipeline.process_text(r['text'], asr_result=r.get('result'), now_ms=i * 10000)
    profiler.disable()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats('cumulative').print_stats(35)
    report['profile'] = stream.getvalue()
    audio = a.session / 'audio.wav'
    report['audio_sha256'] = digest(audio)
    report['model_sha256'] = {str(f.relative_to(a.model)):digest(f) for f in sorted(a.model.rglob('*')) if f.is_file()}
    report['asr'] = []
    for repeat in range(0 if a.skip_audio else a.repeats):
        with wave.open(str(audio)) as w:
            if w.getnchannels()!=1 or w.getsampwidth()!=2:
                p.error('Need mono PCM16 audio.')
            rate = w.getframerate()
            duration = w.getnframes()/rate
            start = time.perf_counter()
            recognizer = SherpaStreamingRecognizer(load_sherpa_recognizer(a.model, sample_rate=rate, num_threads=a.threads),rate)
            load = time.perf_counter()-start
            start = time.perf_counter()
            finals = []
            chunks = []
            while data := w.readframes(rate//2):
                t = time.perf_counter()
                final = recognizer.AcceptWaveform(data)
                chunks.append(1000*(time.perf_counter()-t))
                if final:
                    finals.append(json.loads(recognizer.Result()))
            elapsed = time.perf_counter()-start
            report['asr'].append(dict(repeat=repeat,duration_seconds=duration,load_seconds=load,processing_seconds=elapsed,rtf=elapsed/duration,max_chunk_ms=max(chunks),finals=finals,trailing_partial=json.loads(recognizer.PartialResult())))
            print('ASR repeat',repeat+1,'RTF',round(elapsed/duration,3),flush=True)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved',a.output,flush=True)

if __name__ == '__main__':
    main()
