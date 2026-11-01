import assert from "node:assert/strict";
import test from "node:test";
import { PromptDraftSession } from "../src/prompt-draft-session.ts";

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function setup() {
  const local = new Map();
  const storage = { getItem: (key) => local.get(key) ?? null, setItem: (key, value) => local.set(key,value), removeItem: (key) => local.delete(key) };
  const server = { version:null, markdown:"原文", base_sha256:"a".repeat(64), active_prompt:"原文", active_sha256:"a".repeat(64), dirty:false, conflict:false };
  const events = [], controls = {};
  let changes = 0;
  const transport = {
    load: async () => ({...server}),
    save: async (body) => {
      events.push(["save",body.markdown]);
      if (controls.failSave) throw Object.assign(new Error("network"),{status:503});
      if (body.version!==server.version && body.markdown!==server.markdown) throw Object.assign(new Error("conflict"),{status:409});
      server.markdown=body.markdown;
      server.version=String(Number(server.version??0)+1);
      server.dirty=server.markdown!==server.active_prompt;
      const value={...server};
      if (controls.lostSave) {controls.lostSave=false;throw Object.assign(new Error("lost ack"),{status:503});}
      if (controls.save) {const held=controls.save;delete controls.save;return held.promise.then(()=>value);}
      return value;
    },
    apply: async () => {
      events.push(["apply",server.markdown]);
      if (server.dirty) changes++;
      server.active_prompt=server.markdown;
      server.base_sha256=server.active_sha256="b".repeat(64);
      server.dirty=false;
      const value={draft:{...server},account:{id:1,prompt:server.markdown},changed:true};
      if (controls.lostApply) {controls.lostApply=false;throw Object.assign(new Error("lost ack"),{status:503});}
      if (controls.apply) {const held=controls.apply;delete controls.apply;return held.promise.then(()=>value);}
      return value;
    },
    resolve: async (body) => {server.markdown=body.markdown;server.version="resolved";server.conflict=false;server.dirty=server.markdown!==server.active_prompt;return {...server};},
  };
  let text="原文",composing=false;
  const controller=new PromptDraftSession("1","1",transport,storage,()=>{},()=>{},"test-tab");
  await pause(0);
  controller.bind({read:()=>text,focus:()=>{},composing:()=>composing,normalize:(v)=>v});
  controller.focus();
  const change=(value)=>{text=value;controller.change(value);};
  return {controller,server,events,controls,local,storage,transport,change,
    setText:(value)=>{text=value;},setComposing:(value)=>{composing=value;},changes:()=>changes};
}

test("draft debounce coalesces typing and does not apply before finish",async()=>{
  const f=await setup();
  try {
    f.change("一");await pause(300);f.change("一二");await pause(300);f.change("一二三");
    await pause(1600);
    assert.deepEqual(f.events,[["save","一二三"]]);
    assert.equal(f.server.active_prompt,"原文");
    assert.equal(await f.controller.finish(),true);
    assert.equal(f.server.active_prompt,"一二三");
    assert.equal(f.changes(),1);
  } finally {f.controller.dispose();}
});

test("slow save receipt cannot overwrite continuous input",async()=>{
  const f=await setup();
  try {
    const gate=deferred();f.controls.save=gate;f.change("第一稿");
    const flight=f.controller.persist();await pause(0);
    f.change("第一稿后继续输入");
    assert.equal(f.controller.state.markdown,"第一稿后继续输入");
    gate.resolve();await flight;
    assert.equal(f.controller.state.markdown,"第一稿后继续输入");
    assert.equal(await f.controller.finish(),true);
    assert.equal(f.server.active_prompt,"第一稿后继续输入");
    assert.equal(f.changes(),1);
  } finally {f.controller.dispose();}
});

test("composition pauses requests and finishes only after committed characters",async()=>{
  const f=await setup();
  try {
    f.controller.compositionStart();f.setComposing(true);f.setText("原文中文候选");
    f.controller.change("原文中文候选");
    const ending=f.controller.finish();await pause(1600);
    assert.equal(f.events.length,0);
    f.setComposing(false);f.setText("原文中文");f.controller.compositionEnd();
    assert.equal(await ending,true);
    assert.equal(f.server.active_prompt,"原文中文");
  } finally {f.controller.dispose();}
});

test("concurrent finish requests publish exactly once",async()=>{
  const f=await setup();
  try {
    f.change("结束的稿件");
    const values=await Promise.all([f.controller.finish(),f.controller.finish(),f.controller.finish()]);
    assert.deepEqual(values,[true,true,true]);
    assert.equal(f.events.filter(([kind])=>kind==="apply").length,1);
    assert.equal(f.changes(),1);
  } finally {f.controller.dispose();}
});

test("typing during an apply retains new content and serializes the next draft",async()=>{
  const f=await setup();
  try {
    const gate=deferred();f.controls.apply=gate;f.change("第一轮");
    const ending=f.controller.finish();await pause(0);
    f.controller.focus();f.change("第二轮继续输入");
    const next=f.controller.persist();await pause(0);
    assert.deepEqual(f.events.map(([kind])=>kind),["save","apply"]);
    gate.resolve();assert.equal(await ending,false);await next;
    assert.equal(f.controller.state.markdown,"第二轮继续输入");
    assert.equal(f.server.active_prompt,"第一轮");
    assert.equal(await f.controller.finish(),true);
    assert.equal(f.server.active_prompt,"第二轮继续输入");
    assert.equal(f.changes(),2);
  } finally {f.controller.dispose();}
});

test("failed save preserves local recovery and retry can finish",async()=>{
  const f=await setup();
  try {
    f.controls.failSave=true;f.change("断网草稿");
    assert.equal(await f.controller.finish(),false);
    assert.equal(f.server.active_prompt,"原文");
    assert.equal(f.local.size,1);
    assert.match(f.controller.state.error,/保留/);
    delete f.controls.failSave;await f.controller.retry();
    assert.equal(f.server.active_prompt,"断网草稿");
    assert.equal(f.local.size,0);
  } finally {f.controller.dispose();}
});

test("lost save and apply receipts are reconciled without duplicate effects",async()=>{
  const f=await setup();
  try {
    f.controls.lostSave=true;f.controls.lostApply=true;f.change("可核对的结果");
    assert.equal(await f.controller.finish(),true);
    assert.equal(f.server.active_prompt,"可核对的结果");
    assert.equal(f.changes(),1);
    assert.equal(f.controller.state.error,"");
  } finally {f.controller.dispose();}
});

test("external changes conflict without changing the editor or overwriting active text",async()=>{
  const f=await setup();
  try {
    f.change("我的草稿");f.server.active_prompt="他处更新";f.server.active_sha256="c".repeat(64);
    f.controller.observeActive("他处更新");
    assert.equal(f.controller.state.conflict,true);
    assert.equal(f.controller.state.markdown,"我的草稿");
    assert.equal(await f.controller.finish(),false);
    assert.equal(f.events.length,0);
  } finally {f.controller.dispose();}
});

test("refresh restores the local tail while user and tab identities stay isolated",async()=>{
  const f=await setup();
  f.change("未等到服务器的最后输入");f.controller.dispose();
  const restored=new PromptDraftSession("1","1",f.transport,f.storage,()=>{},()=>{},"test-tab");
  const other=new PromptDraftSession("1","2",f.transport,f.storage,()=>{},()=>{},"test-tab");
  const otherTab=new PromptDraftSession("1","1",f.transport,f.storage,()=>{},()=>{},"another-tab");
  await pause(0);
  assert.equal(restored.state.markdown,"未等到服务器的最后输入");
  assert.equal(other.state.markdown,"原文");
  assert.equal(otherTab.state.markdown,"原文");
  restored.dispose();other.dispose();otherTab.dispose();
});

test("profile recovery is isolated from assistant prompt and other WeChat identities",async()=>{
  const f=await setup();
  f.change("助手尾稿");f.controller.dispose();
  const profile=new PromptDraftSession("1","1",f.transport,f.storage,()=>{},()=>{},"wxid-a:test-tab","owner-profile");
  await pause(0);
  assert.equal(profile.state.markdown,"原文");
  profile.bind({read:()=>"原文",focus:()=>{},composing:()=>false,normalize:v=>v});
  profile.focus();profile.change("号主资料尾稿");profile.dispose();
  const restored=new PromptDraftSession("1","1",f.transport,f.storage,()=>{},()=>{},"wxid-a:test-tab","owner-profile");
  const different=new PromptDraftSession("1","1",f.transport,f.storage,()=>{},()=>{},"wxid-b:test-tab","owner-profile");
  await pause(0);
  assert.equal(restored.state.markdown,"号主资料尾稿");
  assert.equal(different.state.markdown,"原文");
  restored.dispose();different.dispose();
});
