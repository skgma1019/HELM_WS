const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.listeners = {};
    this.className = ''; this.textContent = '';
    this.classList = { add() {}, remove() {} };
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  set innerHTML(value) { this.children = []; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  querySelector() { return new Element(); }
}

const elements = {};
const context = {
  document: {
    getElementById: id => elements[id] ??= new Element(),
    createElement: () => new Element(), addEventListener() {},
  },
  window: {}, location: { protocol: 'https:', host: 'localhost' },
  WebSocket: class { addEventListener() {} },
  MapView: class {
    loadMap() {}
    setMapMeta(meta) { this.meta = meta; }
    reloadImage() { this.reloadCount = (this.reloadCount || 0) + 1; }
    setEvents(events) { this.events = events; }
    setZones(zones) { this.zones = zones; }
    updateRobot() {}
  },
  setTimeout() {}, setInterval() {}, console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/index.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1], context);
const run = source => vm.runInContext(source, context);
run(`handleSnapshot({events:[
  {id:'a',station_id:'A',station_name:'Station A',verdict:'ANOMALY',severity:'caution',ts:Date.now()/1000,vision_score:.6,vision_threshold:.5},
  {id:'b',station_id:'B',verdict:'NORMAL',severity:'info',ts:Date.now()/1000},
  {id:'old',station_id:'C',verdict:'ANOMALY',severity:'danger',acked:true,ts:Date.now()/1000-172800}
]})`);
assert.equal(elements.alertList.children.length, 2);
assert.equal(elements.tileUnacked.textContent, 1);
assert.equal(elements.tile24h.textContent, 1);
assert.equal(elements.tileStations.textContent, '2/3');
elements.showAllToggle.checked = true;
elements.showAllToggle.listeners.change();
assert.equal(elements.alertList.children.length, 3);
run(`applyAck('a','dashboard');openModal('a')`);
assert.equal(elements.tileUnacked.textContent, 0);
assert.equal(elements.modalTitle.textContent, 'Station A (A)');
assert.equal(run(`visionRatio({vision_score:1,vision_threshold:0})`), '-');
run(`handleNewEvent({event:{id:'c',station_id:'C',verdict:'NORMAL',severity:'info',ts:Date.now()/1000}})`);
assert.equal(elements.tileStations.textContent, '3/3');
assert.match(elements.alertList.children[0].className, /new/);
elements.showAllToggle.checked = false;
elements.showAllToggle.listeners.change();
assert.equal(elements.alertList.children.length, 2);
assert.equal(run('mapView.events.length'), 4);
assert.equal(run('Array.isArray(mapView.zones)'), true);
run(`dispatchMessage({type:'map_updated', map:{image:'map.pgm', resolution:.2, origin_x:1, origin_y:2, width:8, height:6}})`);
assert.equal(run('mapView.meta.image'), 'map.pgm');
assert.equal(run('mapView.reloadCount'), 1);
run(`handleSnapshot({events:[{id:'b',station_id:'B',verdict:'NORMAL',ts:Date.now()/1000}]})`);
assert.equal(elements.alertList.children[0].className, 'alert-empty');

const mapContext = {};
vm.createContext(mapContext);
vm.runInContext(fs.readFileSync('static/map.js', 'utf8'), mapContext);
vm.runInContext(`
  const view = Object.create(MapView.prototype);
  view.worldToScreen = (x,y) => [x,y];
  view.zones = [{name:'keepout', points:[[0,0],[1,0],[1,1]]}];
  view.events = [{x:1,y:2,station_id:'A',verdict:'NORMAL'},
    {x:3,y:4,verdict:'ANOMALY',severity:'caution',ts:Date.now()/1000}];
  let diamonds=0, arcs=0, labels=0, zoneLabels=0;
  const ctx = new Proxy({}, {get:(_,key)=>()=>{
    if(key==='closePath') diamonds++;
    if(key==='arc') arcs++;
    if(key==='fillText') { labels++; zoneLabels++; }
  },set:()=>true});
  view._drawZones(ctx);
  view._drawEvents(ctx);
  if(diamonds!==2 || arcs!==5 || labels!==2 || zoneLabels!==2) throw Error('marker regression');
`, mapContext);
console.log('frontend regression checks passed');
