const assetUrl = name => `${import.meta.env.BASE_URL}assets/${name}`;
export const people = [
 {key:"user",name:"User",image:assetUrl("user-avatar.svg"),position:"50% 50%",summary:"记录生活与想法",detail:"从一段对话开始。"},
 {key:"assistant",name:"AI",image:assetUrl("assistant-avatar.svg"),position:"50% 50%",summary:"一起整理值得留下的内容",detail:"在设置中连接你使用的模型。"}
];
export const defaultCoverSettings = {tagline:"让记忆有出处，让故事慢慢形成。",fadeStart:62,portraitHazeEnabled:true,compositionEnabled:false};
export const coverSettingStorageKeys = {
  tagline: "serein.awake.tagline",
  fadeStart: "serein.awake.fadeStart",
  portraitHazeEnabled: "serein.awake.portraitHaze",
  compositionEnabled: "serein.awake.composition",
};

export const defaultCompositionItems = [
  { id: "top-bar", label: "右上横条", kind: "black-block", layer: "front", x: 90.5, y: 21.5, width: 76, height: 10 },
  { id: "left-block", label: "左侧横条", kind: "black-block", layer: "behind", x: 21.5, y: 46, width: 98, height: 17 },
  { id: "left-frame", label: "左侧细框", kind: "black-frame", layer: "front", x: 29.4, y: 62, width: 188, height: 58 },
  { id: "white-chip", label: "中央白块", kind: "white-block", layer: "front", x: 48.8, y: 47, width: 24, height: 18 },
  { id: "right-block", label: "右侧横条", kind: "black-block", layer: "behind", x: 60.6, y: 45, width: 82, height: 22 },
  { id: "right-frame", label: "右侧细框", kind: "white-frame", layer: "front", x: 75.8, y: 56.5, width: 158, height: 56 },
  { id: "right-bar", label: "右下横条", kind: "black-block", layer: "front", x: 73.9, y: 66, width: 96, height: 19 },
  { id: "lower-mark", label: "左下竖条", kind: "black-block", layer: "front", x: 10.5, y: 74, width: 13, height: 27 },
];

export const compositionPresets = {
  "black-block": { label: "黑块", width: 82, height: 18 },
  "white-block": { label: "白块", width: 42, height: 20 },
  "black-frame": { label: "黑框", width: 150, height: 54 },
  "white-frame": { label: "白框", width: 150, height: 54 },
};
