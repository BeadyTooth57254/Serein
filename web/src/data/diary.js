import { identityName } from "../storage/instanceStore.js";
export const defaultDiaryEntries = [];
export const defaultDiaryCommentIdentity = {get author(){return identityName("user");},role:"user"};
export const defaultDarkroom = {
  unlockAt: "",
  title: "暗房",
  get lockedTitle() { return `${identityName("assistant")} 锁了门。`; },
  lockedQuestion: "",
  lockedCopy: "到约定的时间再打开。",
};
