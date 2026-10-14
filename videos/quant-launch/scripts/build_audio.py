"""Mandarin narration with model-compatible phonemes and an original score.

No credentials or paid audio API are needed. Keep the provider replaceable.
"""
import json
from pathlib import Path
import subprocess
import wave
import argparse
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RATE = 48000


def run(command):
    result=subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-1600:])


def length(path):
    return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration',
                                          '-of','default=nw=1:nk=1',str(path)], text=True))


def music(narration_info):
    """Original harmony, bass, arpeggio and drums; fixed seed for reproducibility."""
    total = int(60*RATE)
    stereo = np.zeros((total, 2), dtype=np.float64)
    rng = np.random.default_rng(420)
    def add(start, sound, level=1., pan=0.):
        i = round(start*RATE); n = min(len(sound),total-i)
        if n <= 0: return
        stereo[i:i+n,0] += sound[:n]*level*np.sqrt((1-pan)/2)
        stereo[i:i+n,1] += sound[:n]*level*np.sqrt((1+pan)/2)
    def note(midi, seconds, kind='pad'):
        t=np.arange(round(seconds*RATE))/RATE
        f=440*2**((midi-69)/12)
        tone=np.sin(2*np.pi*f*t)+.23*np.sin(2*np.pi*2*f*t)+.09*np.sin(2*np.pi*3*f*t)
        attack=.12 if kind=='pad' else .006
        env=np.minimum(t/attack,1)*np.minimum((seconds-t)/(.25 if kind=='pad' else .03),1)
        if kind!='pad':env*=np.exp(-t/(.21 if kind=='arp' else .45))
        return tone*env
    chords=[[57,60,64,67],[53,57,60,64],[48,55,60,64],[55,59,62,67]]
    for bar in range(30):
        start=bar*2; chord=chords[(bar//2)%4]
        # A 4-beat sidechain-shaped pad gives the beat room without voice masking.
        t=np.arange(int(2.15*RATE))/RATE
        duck=.38+.62*(1-np.exp(-(t%.5)*11))
        for k,midi in enumerate(chord): add(start,note(midi+12,2.15)*duck,.024,(-.45 if k%2 else .45))
        for b in range(4):
            beat=start+b*.5
            add(beat,note(chord[0]-12,.42,'bass'),.15)
            if bar < 2 or bar >= 4:
                t=np.arange(round(.25*RATE))/RATE
                phase=2*np.pi*(46*t+(145-46)*.028*(1-np.exp(-t/.028)))
                kick=np.sin(phase)*np.exp(-t/ .062)
                add(beat,kick,.40)
            if b%2:
                n=round(.13*RATE); t=np.arange(n)/RATE
                noise=rng.normal(0,1,n); high=noise-np.concatenate(([0],noise[:-1]))
                add(beat,high*np.exp(-t/.024),.06)
        for step in range(16):
            at=start+step*.125
            t=np.arange(round(.055*RATE))/RATE
            noise=rng.normal(0,1,len(t)); high=noise-np.concatenate(([0],noise[:-1]))
            add(at,high*np.exp(-t/.009),.022 if step%2 else .029,.2*(-1 if step%2 else 1))
            if bar>=2:
                midi=chord[(step+(bar%2))%4]+24
                add(at,note(midi,.28,'arp'),.022 if bar<9 else .034,.45*np.sin(step*1.4))
    for at in [0,7,18,29,40,51]:
        t=np.arange(round(.65*RATE))/RATE
        add(at,np.sin(2*np.pi*(36*t+7*(1-np.exp(-t*5))))*np.exp(-t/ .20),.24)
    fade=np.minimum(np.arange(total)/RATE/.12,1)*np.minimum((60-np.arange(total)/RATE)/1.6,1)
    stereo=np.tanh(stereo*1.6)*fade[:,None]
    path=ROOT/'assets/audio/original-score.wav'
    with wave.open(str(path),'wb') as out:
        out.setnchannels(2);out.setsampwidth(2);out.setframerate(RATE)
        out.writeframes((stereo*27000).astype('<i2').tobytes())
    target=path.with_suffix('.mp3')
    run(['ffmpeg','-y','-i',str(path),'-c:a','libmp3lame','-b:a','192k',str(target)])
    (ROOT/'assets/audio/LICENSE.md').write_text('# Audio provenance\n\n'
        'original-score.mp3: Original synthesized music composed for this project by build_audio.py. '
        'No third-party music recordings or samples. 120 BPM, 60 seconds.\n\n'
        f'Narration: {narration_info}. '
        'Base-model license: Apache-2.0, https://huggingface.co/hexgrad/Kokoro-82M . '
        'The previously configured Volcengine provider returned HTTP 403 and was not used.\n')
    return str(target.relative_to(ROOT))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--provider',choices=['kokoro','macos'],default='kokoro')
    parser.add_argument('--voice',default='zf_xiaobei')
    parser.add_argument('--speed',type=float,default=1.0)
    args=parser.parse_args()
    model=None
    if args.provider=='kokoro':
        from kokoro_onnx import Kokoro
        from misaki.zh import ZHG2P
        import soundfile as sf
        base=Path.home()/'.cache/hyperframes/tts'
        model=Kokoro(str(base/'models/kokoro-v1.0.onnx'),str(base/'voices/voices-v1.0.bin'))
        # eSpeak cmn phonemes do not match Kokoro's Mandarin training alphabet.
        # Misaki converts Chinese to the IPA/tones expected by this model.
        phonemizer=ZHG2P()
    spec=json.loads((ROOT/'tts/tts-lines.json').read_text())
    folder=ROOT/'assets/audio'; (folder/'segments').mkdir(parents=True,exist_ok=True)
    rows=[];voices=[]
    for index,scene in enumerate(spec['scenes']):
        segments=[]
        for line in scene['lines']:
            source=folder/'segments'/(line['id']+'.aiff')
            output=source.with_suffix('.wav')
            text=line['text'].replace('Quant','宽特').replace('AI','人工智能')
            settings={'text':line['text'],'speech_text':text,'provider':args.provider,
                      'voice':args.voice if model else 'Ting-Ting (Enhanced)','rate':160,'speed':args.speed,
                      'phonemizer':'misaki-zh-v1.0' if model else 'macos-native'}
            config=source.with_suffix('.json')
            if not output.exists() or not config.exists() or json.loads(config.read_text())!=settings:
                if model:
                    phonemes,_=phonemizer(text)
                    samples,rate=model.create(phonemes,voice=settings['voice'],speed=settings['speed'],is_phonemes=True)
                    sf.write(str(source),samples,rate,format='AIFF')
                else:
                    run(['say','-v',settings['voice'],'-r',str(settings['rate']),'-o',str(source),text])
                run(['ffmpeg','-y','-i',str(source),'-af','silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,loudnorm=I=-16:LRA=5:TP=-1.5','-ar','48000',str(output)])
                config.write_text(json.dumps(settings,ensure_ascii=False))
            segments.append((line,output,length(output)))
            print(json.dumps({'line':line['id'],'seconds':round(segments[-1][2],3),'voice':settings['voice']},ensure_ascii=False),flush=True)
        duration=scene['end']-scene['start']
        spoken=sum(row[2] for row in segments)+.16*(len(segments)-1)
        if spoken>duration-.02:
            ratio=sum(r[2] for r in segments)/(duration-.3-.16*(len(segments)-1))
            if ratio>1.2:raise ValueError(f"{scene['id']} needs shorter copy; compression ratio {ratio:.2f}")
            fitted=[]
            for line,path,seconds in segments:
                target=path.with_name(path.stem+'-fit.wav')
                run(['ffmpeg','-y','-i',str(path),'-af',f'atempo={ratio:.6f}',str(target)])
                fitted.append((line,target,length(target)))
            segments=fitted
            spoken=sum(r[2] for r in segments)+.16*(len(segments)-1)
        padding=.02 if index==0 else min(.5,(duration-spoken)/2)
        filters=[];streams=[];cursor=padding
        command=['ffmpeg','-y']
        for i,(line,path,seconds) in enumerate(segments):
            command+=['-i',str(path)]
            filters.append(f'[{i}:a]adelay={round(cursor*1000)}:all=1[a{i}]');streams.append(f'[a{i}]')
            rows.append({'id':line['id'],'text':line['text'],'path':str(path.relative_to(ROOT)),
                         'duration':round(seconds,3),'start':round(scene['start']+cursor,3),
                         'end':round(scene['start']+cursor+seconds,3),'scene':scene['id']})
            cursor+=seconds+.16
        filters.append(''.join(streams)+f'amix=inputs={len(segments)}:normalize=0,apad,atrim=0:{duration}[out]')
        out=folder/f'frame-{index+1:02d}.wav'
        command+=['-filter_complex',';'.join(filters),'-map','[out]','-ar','48000','-c:a','pcm_s16le',str(out)];run(command)
        voices.append({'frame':index+1,'path':str(out.relative_to(ROOT)),'duration_s':duration,'source':'local-'+args.provider+'-tts'})
    narration_info=(f'local Kokoro-82M / Kokoro ONNX, voice {args.voice}, Misaki Mandarin G2P'
                    if model else 'macOS Ting-Ting Enhanced (review only)')
    bgm=music(narration_info)
    (ROOT/'assets/audio/tts-meta.json').write_text(json.dumps({'provider':args.provider+'-local','voice':args.voice if model else 'Ting-Ting Enhanced','phonemizer':'misaki-zh-v1.0' if model else 'macos-native','timeline_duration':60,'lines':rows},ensure_ascii=False,indent=2))
    (ROOT/'audio_meta.json').write_text(json.dumps({'voices':voices,'bgm':{'path':bgm,'volume':.12,'source':'original-local-synthesis'},'sfx':[],'total_duration_s':60},ensure_ascii=False,indent=2))
    print(json.dumps({'voice_lines':len(rows),'voice_duration_sum':round(sum(r['duration'] for r in rows),2),'music_seconds':length(ROOT/bgm),'timeline':60,'provider':args.provider+'-local'}))


if __name__=='__main__':main()
