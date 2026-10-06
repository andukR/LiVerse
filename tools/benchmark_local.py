'''Replay timing test; no microphone or Holyrics requests.'''
import argparse
import cProfile
import hashlib
import io
import json
import math
import pstats
import re
import os
import shutil
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packages/bible_parser_core/src'))
from bible_parser_core.live_pipeline import LiveReferencePipeline
from bible_parser_core import parser as reference_parser
from bible_parser_core.sherpa_streaming import load_sherpa_recognizer, SherpaStreamingRecognizer
from bible_parser_core.version import __version__


def church_reading_text(*, diagnostic_test=True):
    """Use the shipped Bible, so the reading matches the recognizer's text base."""
    bible = reference_parser.bible_map(reference_parser.DEFAULT_BIBLE)
    passage = '\n\n'.join(bible['Лука'][15][verse] for verse in range(11, 25))
    preparation = '''Закройте другой LV. В тестовом LV нажмите «Начать распознавание», дождитесь
готовности. Настройки теста временные: диагностика, адреса и текст, полуавтомат,
УПС, показ по одному стиху и временный показ на 5 секунд.'''
    if not diagnostic_test:
        preparation = '''Во вкладке «Настройки» включите диагностику производительности,
выберите осторожный полуавтомат, поиск адресов и текста стихов, УПС,
показ длинного отрывка по одному стиху и время временного показа 5 секунд.
Нажмите «Начать распознавание» и дождитесь готовности модели.
Это окно показывает текст; настройки и запуск распознавания выполняет оператор.'''
    return f'''LV {__version__}: текст для проверки (читайте текст вне блоков в квадратных скобках)

[Подготовка: запустите Holyrics, откройте план проповеди, выберите микрофон.
{preparation}
Читайте спокойно 7–10 минут, не ускоряйтесь.
Короткие паузы после предложений нужны для завершения фраз.
Подтверждайте предложения LV и следите за реальным изображением в Holyrics.]

[Один адрес. Дождитесь показа, затем истечения времени временного слайда.]
Откроем Евангелие от Иоанна, третью главу, шестнадцатый стих.

[Произнесите текст после адреса и проверьте, что он не открывается повторно.]
{bible['Иоанн'][3][16]}

[Обычная речь: она не должна вызывать случайный показ. После каждой точки — короткая пауза.]
Сегодня мы собрались вместе, чтобы молиться и поддерживать друг друга.
Каждому бывает нужна помощь, особенно когда приходится решать трудные вопросы.
Мы можем внимательно выслушать человека и найти время для разговора.
Иногда гораздо важнее проявить терпение, чем сразу предложить готовый ответ.
В повседневных делах легко забыть о людях, которые находятся рядом.
Давайте замечать тех, кому сейчас одиноко и кто нуждается в нашем внимании.
Небольшой добрый поступок может изменить настроение всей семьи.
Пусть наши слова будут понятными, спокойными и доброжелательными.
Будем учиться благодарить за помощь и признавать собственные ошибки.
Нам не нужно спешить, когда мы вместе ищем правильное решение.

[Проверка окна уточнения главы. Эта фраза должна вызвать запрос главы.
Не отвечайте на него около 10 секунд: пока окно открыто, читайте следующий
обычный текст. LV должен продолжать принимать фразы без растущей очереди.
Затем закройте уточнение клавишей Esc; показывать стих не нужно.]
Иаков, шестой стих.
Иногда мы называем только часть нужной информации, а затем спокойно уточняем её.
Пока один вопрос остаётся открытым, разговор и распознавание продолжаются.

[Проверка оформления Holyrics. После запуска LV оставьте активным план проповеди,
затем переключите Holyrics на слайд песни. Подтверждайте каждый адрес отдельно;
проверьте, что все три временных показа появились и читаемы, без отказов API.
Если быстрый показ завершился и план вернулся, снова откройте песню перед адресом.]
Первое послание к Коринфянам, одиннадцатая глава, двадцать третий стих.
{bible['1 Коринфянам'][11][23]}
Первое послание к Коринфянам, одиннадцатая глава, с двадцать третьего по двадцать четвёртый стих.
{bible['1 Коринфянам'][11][23]}
{bible['1 Коринфянам'][11][24]}
Первое послание к Коринфянам, одиннадцатая глава, с двадцать пятого по двадцать шестой стих.
{bible['1 Коринфянам'][11][25]}
{bible['1 Коринфянам'][11][26]}

[Список из четырёх ссылок: произнесите подряд, с короткими паузами, подтвердите
полный список. На экране должны быть все четыре адреса.]
Запишем четыре места для чтения: Евангелие от Иоанна, третья глава, шестнадцатый стих;
Послание к Римлянам, восьмая глава, первый стих;
Послание к Ефесянам, вторая глава, восьмой стих;
Евангелие от Матфея, пятая глава, девятый стих.

[После подтверждения списка сразу объявите длинный отрывок, подтвердите его.
Проверьте, что прежний таймер списка не закрывает новый отрывок.]
Прочитаем Евангелие от Луки, пятнадцатую главу, с одиннадцатого по двадцать четвёртый стих.

[Читайте дальше после появления первого стиха. УПС должен переходить вслед за
чтением. Заголовки и номера добавлять не нужно. Паузы — естественные.]
{passage}

[После завершения отрывка проверьте возврат к плану. Затем новый адрес.]
Евангелие от Иоанна, третья глава, семнадцатый стих.
{bible['Иоанн'][3][17]}

[Если от первой до последней фразы прошло менее пяти минут, ещё раз прочитайте
блок обычной речи, затем длинный отрывок; минимум двадцать непустых фраз.
По окончании помолчите 10 секунд, остановите распознавание.
В обычном LV: «Журналы» → «Выбрать последний» → «Проверить сеанс».
В консольном тесте закройте LV, чтобы получить анализ нового сеанса.]
'''


def church_cpu_ids(topology_root=Path('/sys/devices/system/cpu'), allowed=None):
    """Two distinct physical cores with their available sibling threads."""
    allowed = sorted(os.sched_getaffinity(0) if allowed is None else allowed)
    cores = {}
    for cpu in allowed:
        path = topology_root / f'cpu{cpu}' / 'topology'
        identity = ((path / 'physical_package_id').read_text().strip(),
                    (path / 'core_id').read_text().strip())
        cores.setdefault(identity, []).append(cpu)
    if len(cores) < 2:
        raise ValueError('Для теста нужны два доступных физических ядра.')
    return [cpu for siblings in list(cores.values())[:2] for cpu in siblings]


def verify_church_limits(cpus, percent):
    actual = sorted(os.sched_getaffinity(0))
    if actual != sorted(cpus):
        raise ValueError(f'Ограничение ядер не применено: доступны {actual}, ожидались {cpus}.')
    cgroup = next((line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')), None)
    if cgroup is None:
        raise ValueError('Нужен Linux с cgroup v2 для проверяемого ограничения процессорного времени.')
    quota, period = (Path('/sys/fs/cgroup') / cgroup.lstrip('/') / 'cpu.max').read_text().split()
    if quota == 'max' or abs(100 * int(quota) / int(period) - percent) > 1:
        raise ValueError('Ограничение процессорного времени не применено; тест без него не запускается.')
    return dict(cpus=actual, cpu_percent=100 * int(quota) / int(period),
                quota_period_ms=int(period)/1000, calibrated=False)


def run_church_test(args):
    if not sys.platform.startswith('linux'):
        raise ValueError('Имитация ресурсов предназначена для Linux-ноутбука. На церковном Windows используйте обычный LV и проверку журнала.')
    if not math.isfinite(args.church_cpu_percent) or not 1 <= args.church_cpu_percent <= 400:
        raise ValueError('--church-cpu-percent должен быть от 1 до 400.')
    cpus = [int(v) for v in args.church_cpus.split(',')] if args.church_cpus else church_cpu_ids()
    if sorted(cpus) != sorted(church_cpu_ids(allowed=cpus)) or len(set(cpus)) != len(cpus):
        raise ValueError('--church-cpus должен указывать два разных физических ядра с доступными потоками.')
    if not args.church_worker:
        if not shutil.which('systemd-run') or not shutil.which('taskset'):
            raise ValueError('Нужны уже установленные systemd-run и taskset.')
        command = ['systemd-run', '--user', '--scope', '--quiet',
                   '--property', f'CPUQuota={args.church_cpu_percent:g}%',
                   '--property', 'CPUQuotaPeriodSec=20ms', '--', 'taskset', '-c', ','.join(map(str, cpus)),
                   sys.executable, str(Path(__file__).resolve()), '--church-test', '--church-worker',
                   '--church-cpus', ','.join(map(str, cpus)), '--church-cpu-percent', str(args.church_cpu_percent)]
        if args.check_limits:
            command.append('--check-limits')
        print(f'LV {__version__}. Имитация: CPU {cpus}, общий лимит {args.church_cpu_percent:g}% (100% = время одного логического CPU).', flush=True)
        print('Это не точная копия i3-3220: ограничение не откалибровано; Holyrics вне лимита, память и Windows не моделируются.', flush=True)
        if args.dry_run:
            import shlex
            print(shlex.join(command), flush=True)
            print(church_reading_text(), flush=True)
            return 0
        return subprocess.call(command, cwd=ROOT)
    profile = verify_church_limits(cpus, args.church_cpu_percent)
    print('Применённые ограничения:', json.dumps(profile, ensure_ascii=False), flush=True)
    if args.check_limits:
        return 0
    from tools.liverse_gui import DEFAULT_LOG_DIR, INSTANCE_PORT, list_log_sessions
    from tools.analyze_vosk_probe_logs import check_live_session, format_session_check
    with socket.socket() as probe:
        probe.settimeout(.5)
        if probe.connect_ex(('127.0.0.1', INSTANCE_PORT)) == 0:
            raise ValueError('Сначала полностью закройте уже запущенный LV, затем повторите команду.')
    previous = set(list_log_sessions(DEFAULT_LOG_DIR))
    environment = os.environ.copy()
    environment['LIVERSE_DIAGNOSTIC_TEST_PROFILE'] = json.dumps(profile)
    print(church_reading_text(), flush=True)
    app = subprocess.Popen([sys.executable, str(ROOT/'tools/liverse_gui.py'), '--diagnostic-test'], cwd=ROOT, env=environment)
    try:
        code = app.wait()
    except KeyboardInterrupt:
        app.terminate()
        try:
            app.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise ValueError('LV ещё завершает работу. Остановите распознавание и закройте окно вручную.')
        print('Тест прерван оператором.', flush=True)
        return 130
    sessions = [p for p in list_log_sessions(DEFAULT_LOG_DIR) if p not in previous]
    if code != 0 or not sessions:
        raise ValueError('LV завершился с ошибкой или не создал новый журнал. Тест не подтверждён.')
    reports = []
    for session in reversed(sessions):
        report = check_live_session(session)
        print('\n' + format_session_check(report), flush=True)
        reports.append(report)
    answer = input('\nВсе адреса, четыре пункта списка, переходы УПС и возврат к плану были правильными на экране? [да/нет/не проверено]: ').strip().casefold()
    if answer not in ('да', 'д', 'yes', 'y'):
        print('НЕ РЕКОМЕНДУЕТСЯ: оператор заметил ошибку показа.' if answer in ('нет', 'н', 'no', 'n') else 'НЕДОСТАТОЧНО ДАННЫХ: изображение не проверено.', flush=True)
        return 1 if answer in ('нет', 'н', 'no', 'n') else 2
    return 1 if any(r['status'] == 'failed' for r in reports) else 2 if any(r['status'] == 'insufficient' for r in reports) else 0

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path)
    p.add_argument('--model', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--church-test', action='store_true', help='Launch live LV with bounded laptop CPU resources and a console reading guide.')
    p.add_argument('--print-church-text', action='store_true', help='Only print the reading guide; do not launch LV.')
    p.add_argument('--church-cpus', help='Logical CPU IDs; default: sibling threads of two distinct physical cores.')
    p.add_argument('--church-cpu-percent', type=float, default=100, help='Total CPU time budget; 100%% equals one logical CPU. Uncalibrated stress profile.')
    p.add_argument('--dry-run', action='store_true', help='Preview church-test command without launching it.')
    p.add_argument('--check-limits', action='store_true', help='Verify church-test CPU limits and exit without opening LV.')
    p.add_argument('--church-worker', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--threads', type=int, default=1)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--regex-cache', type=int, default=0, help='Diagnostic-only Python regex cache size; 0 keeps default.')
    p.add_argument('--skip-audio', action='store_true')
    p.add_argument('--skip-profile', action='store_true', help='Skip the slower profiling pass; measured parser passes still run.')
    a = p.parse_args()
    if a.print_church_text:
        print(church_reading_text(), flush=True)
        return 0
    if a.church_test:
        try:
            return run_church_test(a)
        except (OSError, ValueError, EOFError) as exc:
            print(f'Тест не выполнен: {exc}', file=sys.stderr, flush=True)
            return 2
        except KeyboardInterrupt:
            print('Тест прерван оператором.', flush=True)
            return 130
    if a.session is None or a.output is None:
        p.error('--session and --output are required for an offline benchmark.')
    if a.output.exists() or a.repeats < 1:
        p.error('Use a new output file and positive repeat count.')
    if not a.skip_audio and a.model is None:
        p.error('--model is required unless --skip-audio is used.')
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
            timings.append(dict(repeat=repeat, ts=r.get('ts'), text=r['text'], ms=1000*(time.perf_counter()-start), reference=(result.get('parsed') or {}).get('ref'), payload=result))
        print('Parser repeat', repeat+1, 'complete', flush=True)
    report['parser'] = timings
    cache = getattr(reference_parser, '_cached_book_candidates', None)
    if cache is not None:
        report['book_candidate_cache'] = cache.cache_info()._asdict()
    if not a.skip_profile:
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
    report['model_sha256'] = {str(f.relative_to(a.model)):digest(f) for f in sorted(a.model.rglob('*')) if f.is_file()} if a.model else {}
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
    raise SystemExit(main())
