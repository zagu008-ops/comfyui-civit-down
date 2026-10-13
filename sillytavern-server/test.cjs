const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {Broker,targetUrl}=require('./broker.cjs');
(async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'cosji-broker-')),values=new Map(),calls=[];
 let count=0;
 const secrets={writeSecret:(_,key,value)=>{const id='secret-'+(++count);values.set(id,value);return id;},readSecret:(_,key,id)=>values.get(id)};
 const broker=new Broker({secrets,fetchImpl:async(url,options)=>{calls.push({url,options});return new Response(JSON.stringify({protocol:1,version:'1.0',connected:true}),{status:200});}});
 const dirs={root},key='private-pairing-key-'.repeat(3);
 try{
  const paired=await broker.pair(dirs,{url:'http://127.0.0.1:8188/',key,name:'测试电脑'});
  assert.equal(broker.list(dirs)[0].url,'http://127.0.0.1:8188');
  assert(!JSON.stringify(broker.list(dirs)).includes(key));assert(!fs.readFileSync(broker.file(dirs),'utf8').includes(key));
  await broker.proxy(dirs,{id:paired.id,route:'inspect',body:{url:'https://civitai.red/models/1'}});
  assert.equal(calls.at(-1).options.headers.Authorization,'Bearer '+key);
  assert.equal(calls.at(-1).options.redirect,'manual');
  await assert.rejects(broker.proxy(dirs,{id:paired.id,route:'local/config'}));
  await assert.rejects(broker.proxy(dirs,{id:paired.id,route:'https://attacker.invalid'}));
  await assert.rejects(broker.proxy(dirs,{id:'other-user-id',route:'capabilities'}));
  assert.throws(()=>targetUrl('http://user:password@localhost:8188'));
  assert.throws(()=>targetUrl('file:///tmp/file'));
  const pair2=await broker.pair(dirs,{url:'http://127.0.0.1:8188',key,name:'改名'});
  assert.equal(pair2.id,paired.id);assert.equal(broker.list(dirs).length,1);
  const record={id:'12345678-abcd-1234-abcd-123456789abc',connectionId:paired.id,plan:{roleName:'乾纱寿叶'},state:'pending'};
  broker.saveImport(dirs,record);assert.equal(broker.imports(dirs).length,1);
  broker.saveImport(dirs,{...record,state:'done'});assert.equal(broker.imports(dirs)[0].state,'done');
  assert.throws(()=>broker.saveImport(dirs,{...record,plan:{token:'credential'}}));
  assert.throws(()=>broker.saveImport(dirs,{...record,connectionId:'unpaired'}));
  await assert.rejects(broker.preview('https://attacker.invalid/private'));
  fs.writeFileSync(path.join(root,'settings.json'),JSON.stringify({extension_settings:{'1011-st':{cosji:{civitCompletedImports:{[record.id]:{jobId:'verified'}}}}}}));
  assert.equal(broker.verifyImport(dirs,{extensionName:'1011-st',id:record.id}).verified,true);
  assert.equal(broker.verifyImport(dirs,{extensionName:'1011-st',id:'absent'}).verified,false);
  assert.throws(()=>broker.verifyImport(dirs,{extensionName:'../../other',id:record.id}));
  console.log('PASS: broker pairing, per-user connections, secret isolation, proxy allowlist and no credential redirects');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
