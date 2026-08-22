// Playful labels rotated by job.js every ROTATION_MS while a job is in
// progress. Keyed by Job.status values from the backend. The progress
// bar carries the real percentage; this is purely UI personality.
//
// Edit freely — additions/removals don't require any code changes.
//
// One phrase set per language (English, Polish, Japanese, Simplified
// Chinese) rather than literal translations of the English lines: the
// band-rehearsal/soundcheck humor is idiomatic, so each language gets its
// own equivalently playful set instead of a word-for-word rendering that
// wouldn't land the same way. getStagePhrases() resolves the set for
// whichever language is active right now.

import { getLanguage } from "./i18n.js";

const PHRASES = {
  en: {
    queued: [
      "Lacing up…",
      "Tuning forks…",
      "Spinning up…",
      "Warming the tubes…",
    ],
    // Downloading is load-in: gear arriving, getting set up, before the
    // sound check starts.
    downloading: [
      "Load-in…",
      "Rolling in flight cases…",
      "Setting up the kit…",
      "Snaking the cables…",
      "Plugging into the patch bay…",
    ],
    // Separating is the long stage — keep the user company with band /
    // sound-check vignettes. Short observational sentences, the kind of
    // thing you'd overhear at a rehearsal.
    separating: [
      "Tuning the bass…",
      "Guitarist checking himself in the mirror…",
      "Pick fell on the floor…",
      "In-ear check…",
      "Mic check, one two…",
      "\"More me in the monitor\"…",
      "Drummer adjusting the snare…",
      "Singer warming up… lalala",
      "Capo on, capo off…",
      "Tightening the lugs…",
      "Tuning the floor tom…",
      "Coiling a cable…",
      "Bassist plugged in backwards…",
      "Pedalboard wiggling…",
      "Tech swapping a 9V battery…",
      "Roadie taping down the setlist…",
      "\"Is this thing on?\"…",
      "Stepping on a fuzz pedal…",
      "Tuning the high E…",
      "Drummer twirling sticks…",
      "Singer sipping tea…",
      "Quick bathroom break…",
      "\"Can I get more vocals?\"…",
      "Snare too snappy…",
      "Levels look good…",
    ],
    default: ["Working on it…"],
  },
  pl: {
    queued: [
      "Wiązanie sznurówek…",
      "Strojenie kamertonu…",
      "Rozkręcanie się…",
      "Nagrzewanie lamp…",
    ],
    downloading: [
      "Rozładunek sprzętu…",
      "Toczenie skrzyń…",
      "Ustawianie perkusji…",
      "Rozwijanie kabli…",
      "Podłączanie do patchbaya…",
    ],
    separating: [
      "Strojenie basu…",
      "Gitarzysta poprawia się w lustrze…",
      "Kostka spadła na podłogę…",
      "Sprawdzanie słuchawek dousznych…",
      "Próba mikrofonu, raz dwa…",
      "\"Więcej mnie na monitorach\"…",
      "Perkusista poprawia werbel…",
      "Wokalista się rozśpiewuje… lalala",
      "Kapodaster wpięty, wypięty…",
      "Dokręcanie naciągów…",
      "Strojenie tom-toma…",
      "Zwijanie kabla…",
      "Basista podłączony na odwrót…",
      "Grzebanie w pedalboardzie…",
      "Technik wymienia baterię 9V…",
      "Roadie przykleja taśmą setlistę…",
      "\"Czy to działa?\"…",
      "Nadepnięcie na przester…",
      "Strojenie górnego E…",
      "Perkusista kręci pałkami…",
      "Wokalista popija herbatę…",
      "Krótka przerwa do toalety…",
      "\"Mogę prosić więcej wokalu?\"…",
      "Werbel zbyt trzaskliwy…",
      "Poziomy wyglądają dobrze…",
    ],
    default: ["Pracujemy nad tym…"],
  },
  ja: {
    queued: [
      "準備中…",
      "音叉を調整中…",
      "エンジン始動…",
      "真空管を温めています…",
    ],
    downloading: [
      "機材の搬入中…",
      "ケースを運んでいます…",
      "ドラムセットを組み立て中…",
      "ケーブルを配線中…",
      "パッチベイに接続中…",
    ],
    separating: [
      "ベースをチューニング中…",
      "ギタリストが鏡で身だしなみチェック…",
      "ピックが床に落ちた…",
      "イヤモニのチェック中…",
      "マイクチェック、ワンツー…",
      "「モニターにもっと自分の音を」…",
      "ドラマーがスネアを調整中…",
      "ボーカルが発声練習中… らららー",
      "カポを付けたり外したり…",
      "ラグボルトを締め直し中…",
      "フロアタムをチューニング中…",
      "ケーブルを巻いています…",
      "ベーシストが逆さまに接続…",
      "ペダルボードをいじっています…",
      "テクニシャンが9V電池を交換中…",
      "ローディーがセットリストをテープで固定中…",
      "「これ、ちゃんと鳴ってる?」…",
      "ファズペダルを踏んでしまった…",
      "1弦をチューニング中…",
      "ドラマーがスティックを回している…",
      "ボーカルがお茶を一口…",
      "ちょっとお手洗いに…",
      "「ボーカルをもう少し上げてもらえますか」…",
      "スネアの音が鋭すぎる…",
      "レベルは良好です…",
    ],
    default: ["作業中…"],
  },
  "zh-Hans": {
    queued: [
      "系鞋带中…",
      "调试音叉…",
      "启动中…",
      "预热电子管…",
    ],
    downloading: [
      "搬运设备中…",
      "推着琴箱进场…",
      "架设鼓组中…",
      "整理线缆中…",
      "接入配线架…",
    ],
    separating: [
      "调试贝斯…",
      "吉他手在照镜子整理造型…",
      "拨片掉地上了…",
      "检查耳返…",
      "麦克风测试,一二…",
      "「监听里再多一点我的声音」…",
      "鼓手在调整军鼓…",
      "主唱在热嗓…啦啦啦",
      "变调夹装上又取下…",
      "拧紧鼓身螺丝…",
      "调试落地嗵鼓…",
      "缠绕线缆中…",
      "贝斯手接反了…",
      "摆弄效果器板…",
      "技术人员在换9V电池…",
      "巡演助理在用胶带固定歌单…",
      "「这个有声音吗?」…",
      "不小心踩到了失真踏板…",
      "调试高音弦…",
      "鼓手转着鼓棒…",
      "主唱喝了口茶…",
      "去下洗手间…",
      "「人声能再大声一点吗」…",
      "军鼓声音太脆…",
      "电平看起来不错…",
    ],
    default: ["处理中…"],
  },
};

export function getStagePhrases() {
  return PHRASES[getLanguage()] || PHRASES.en;
}
