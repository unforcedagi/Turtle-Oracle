#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html=fs.readFileSync(new URL("../app/web/kiosk.html",import.meta.url),"utf8");
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const start=script.indexOf("let SERVER_AUDIO=");
const end=script.indexOf("/* ---------- word-by-word incantation",start);
assert.ok(start>0&&end>start,"voice block not found");
const voiceBlock=script.slice(start,end);

function harness(serverOK){
  const requests=[], browser=[], revoked=[];
  const snd={listeners:{},textContent:"🔊",addEventListener(name,fn){this.listeners[name]=fn;}};
  class Utterance { constructor(text){this.text=text;} }
  class Audio {
    constructor(url){this.url=url;this.src=url;this.paused=false;}
    play(){queueMicrotask(()=>this.onended&&this.onended());return Promise.resolve();}
    pause(){this.paused=true;}
  }
  const speech={cancel(){},getVoices(){return[];},speak(u){browser.push(u.text);if(u.onend)queueMicrotask(u.onend);}};
  const context=vm.createContext({
    console,SPOKEN:true,VOICE:null,$:()=>snd,document:{body:{classList:{add(){},remove(){}}}},
    window:{Audio,speechSynthesis:speech},Audio,speechSynthesis:speech,
    SpeechSynthesisUtterance:Utterance,AbortController,queueMicrotask,
    URL:{createObjectURL:()=>"blob:test",revokeObjectURL:url=>revoked.push(url)},
    fetch:async(_url,opts)=>{requests.push(JSON.parse(opts.body).text);return{
      ok:serverOK,blob:async()=>new Blob(["wav"],{type:"audio/wav"})};},
    Blob,setTimeout:()=>1,clearTimeout(){},
  });
  vm.runInContext(voiceBlock,context);
  return {context,requests,browser,revoked,snd};
}

const neural=harness(true);
await vm.runInContext('new Promise(resolve=>speak("First line. Second line!",resolve))',neural.context);
assert.deepEqual(neural.requests,["First line.","Second line!"]);
assert.deepEqual(neural.browser,[],"browser voice must stay silent when server audio works");
assert.equal(neural.revoked.length,2,"each played object URL must be released");

const fallback=harness(false);
await vm.runInContext('new Promise(resolve=>speak("Still speaks. Still works.",resolve))',fallback.context);
assert.deepEqual(fallback.requests,["Still speaks."],"stop server attempts after the first failure");
assert.deepEqual(fallback.browser,["Still speaks.","Still works."],"browser voice covers every remaining line");

console.log("kiosk voice: line chunking, neural playback, cleanup, and browser fallback passed");
