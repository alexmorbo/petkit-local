import { BASE, api, esc, toast } from './core.js';
import { onAction, onChange } from './delegate.js';
import { help } from './help.js';
import { controlRow } from './entities.js';
import { AI_ENTITY_KEYS, facesOnDevice, fetchDeviceDetail } from './devices.js';

onChange('set-ai', el => setAiEnabled(Number(el.dataset.id), el.checked));
async function setAiEnabled(id, on) {
  const r = await api('devices/' + id + '/ai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ai_enabled: on }),
  });
  toast(r.ok ? 'Saved' : 'Error: ' + (r.error || 'failed'));
  // This toggle decides whether the pets section exists at all, so the view has
  // to be rebuilt — otherwise it saves and nothing visibly happens, which reads
  // as the switch not working.
  loadPets();
}

// ---------------- AI / Pets ----------------
// The device holds at most this many mugshots per pet. Mirrors
// events/store.py::MAX_FACES_PER_PET, and a test asserts the two agree.
const MAX_FACES = 6;

// ---- the tab ---------------------------------------------------------------
async function loadPets() {
  const v = document.getElementById('petsView');
  const [ds, pd, ub, info] = await Promise.all([
    api('devices'),
    api('pets'),
    api('pets/unbound').catch(() => ({})),
    api('info').catch(() => ({})),
  ]);
  const pets = pd.pets || [];
  const unbound = ub.unbound || [];
  const aiDevices = ds.filter(d => d.supports_ai);
  // Full detail per AI device: the summary in /api/devices carries no entities
  // and no ai_enabled. A household has one or two of these, so fetching them
  // together costs nothing.
  const aiDetails = (await Promise.all(aiDevices.map(d => fetchDeviceDetail(d.id)))).filter(
    d => d && !d.error,
  );
  const ourFaceIds = pets.flatMap(p => (p.faces || []).map(f => f.id));

  // Everything below the settings depends on at least one device actually
  // recognising: mugshots feed a matcher, so with every matcher off they are
  // configuration for nothing. `ai_enabled` defaults to on when unset, which is
  // what a device that has never been touched reports.
  // Per device, always: one box can be recognising while another is not, and a
  // pet assigned only to the switched-off one is not being matched by anything.
  // `ai_enabled` defaults to on when unset, which is what a device that has
  // never been touched reports.
  const recognisingIds = aiDetails
    .filter(d => !d.aiInfo || d.aiInfo.ai_enabled !== false)
    .map(d => d.id);
  const recognising = recognisingIds.length > 0;

  v.innerHTML = `
    ${aiDetails.map(d => deviceAiCard(d, ourFaceIds)).join('')}
    ${aiDevices.length ? '' : noAiDeviceCard(info.ai_device_names || [])}
    ${aiDevices.length && !recognising ? recognitionOffCard() : ''}
    ${recognising ? petsSection(pets, ds, aiDevices, recognisingIds) : ''}
    ${aiDevices.length ? importPetsCard(aiDevices) : ''}
    ${recognising && unbound.length ? unboundCard(unbound, pets) : ''}`;
}

// One button per recognising device. PetKit's account holds the reference
// photos its NPU matches against, and the pet id it reports them under.
//
// Asked only when pressed — this reaches out to PetKit and downloads
// photographs, which is not something a page load gets to do.
function importPetsCard(aiDevices) {
  return `<div class="card">
    <h3>Import from PetKit${help(
      'Asks PetKit which pets this device recognises and downloads their reference photos. Each pet arrives under a new local id with PetKit’s own id bound to it, so events the box already recorded under that id get named too. Nothing is sent until you press the button.',
    )}</h3>
    <p class="sub">Brings the mugshots across and names the history already recorded under PetKit's pet ids. Imported pets are called <code>PetKit pet &lt;id&gt;</code> — the name is not in the data a device receives, so rename them afterwards.</p>
    ${aiDevices
      .map(
        d => `<div class="unbound">
      <b>${esc(d.name || '#' + d.id)}</b>
      <span class="grow"></span>
      <button class="mini" data-action="pets-cloud-import" data-id="${esc(d.id)}">Import from PetKit</button>
    </div>`,
      )
      .join('')}
  </div>`;
}

function petsSection(pets, ds, aiDevices, recognisingIds) {
  return `
    <div class="card">
      <div class="row" style="align-items:baseline">
        <h3 style="flex:1;margin:0">Pets</h3>
        <span class="mut">${pets.length} pet${pets.length === 1 ? '' : 's'}</span>
      </div>
      <p class="sub" style="margin-top:8px">
        Give each cat up to ${MAX_FACES} mugshots. The device downloads them and matches
        against them itself — more angles, better recognition.
      </p>
      <div class="newpet">
        <input id="newPetName" placeholder="Name" aria-label="Pet name">
        <input id="newPetWeight" type="number" min="0" step="1" placeholder="Weight, g"
               aria-label="Reference weight in grams (optional)">
        <select id="newPetDevice" aria-label="Assign to device">${aiDevices
          .map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
          .join('')}</select>
        <button class="act" data-action="add-pet">Add pet</button>
      </div>
    </div>
    ${pets.length ? pets.map(p => petCard(p, ds, recognisingIds)).join('') : emptyPets()}`;
}

// Recognition is off everywhere. The pets are still stored — this only hides a
// section that would be configuring a matcher nobody is running, and the toggle
// that changes it is the card directly above.
// No registered device can recognise anything, so there is nothing to
// configure. The product list comes from the server rather than being spelled
// out here, which is what let the old copy go stale when a fountain joined it.
function noAiDeviceCard(names) {
  return `<div class="card"><h3>No AI-capable device</h3>
    <p class="sub" style="margin:0">None of your devices does on-device recognition${
      names.length ? ` — that is ${names.map(esc).join(', ')}` : ''
    }. A device not on that list earns it by asking: the first time one requests face
    data we remember it, which is how the newer YumShare models are recognised despite
    sharing a codename with older ones that have no camera AI.</p></div>`;
}

function recognitionOffCard() {
  return `<div class="card empty">
    <div class="big">\u{1F634}</div>
    <b>Pet recognition is off</b>
    <p class="sub" style="margin:6px 0 0">Your mugshots are kept, but nothing is matching
      against them. Turn <b>Recognise pets</b> back on above to manage them.</p>
  </div>`;
}

// The per-device AI settings. They sit here rather than on the device page
// because they are about the pets: recognition, whether the box listens for
// yowling, whether it reads urine pH. The device page keeps a pointer.
function deviceAiCard(d, ourFaceIds) {
  const ents = (d.entities || []).filter(e => AI_ENTITY_KEYS.includes(e.key));
  const enabled = d.aiInfo ? d.aiInfo.ai_enabled : true;
  return `<div class="card">
    <div class="row" style="align-items:baseline">
      <h3 style="flex:1;margin:0">On-device AI</h3>
      <span class="mut">${esc(d.name)}</span>
    </div>
    <p class="sub" style="margin-top:8px">What this device's camera watches and listens for.</p>
    <div class="ctrls">
      <label class="ctrl"><span>Recognise pets</span><span class="sw"><input type="checkbox" ${
        enabled ? 'checked' : ''
      } data-change="set-ai" data-id="${esc(d.id)}"><span class="sl"></span></span></label>
      ${ents.map(e => controlRow(d.id, e)).join('')}
    </div>
    ${facesOnDevice(d, ourFaceIds)}
  </div>`;
}

function emptyPets() {
  return `<div class="card empty">
    <div class="big">🐈</div>
    <b>No pets yet</b>
    <p class="sub" style="margin:6px 0 0">Add one above, then give it a few mugshots.
      Until then the box records visits without saying who made them.</p>
  </div>`;
}

function petCard(p, ds, recognisingIds) {
  let devIds = [];
  try {
    devIds = JSON.parse(p.device_ids_json || '[]');
  } catch (e) {
    /* a pet linked to nothing reads better than a broken card */
  }
  // A chip per device this pet is assigned to, dimmed when that particular box
  // is not recognising — the assignment is still real, it is just not doing
  // anything there. Without this a pet on two devices looks equally active on
  // both when only one is matching.
  const off = id => Array.isArray(recognisingIds) && !recognisingIds.includes(id);
  const chips = devIds
    .map(id => {
      const d = ds.find(x => x.id === id);
      const name = esc(d ? d.name : '#' + id);
      return off(id)
        ? `<span class="badge off" title="Recognition is off on this device">${name} · off</span>`
        : `<span class="badge">${name}</span>`;
    })
    .join(' ');
  const faces = p.faces || [];
  const full = faces.length >= MAX_FACES;

  return `<div class="card petcard">
    <div class="pet-head">
      <b class="pet-name" data-action="rename-pet" data-id="${esc(p.id)}"
         title="Click to rename">${esc(p.name)}</b>
      <span class="pet-weight" data-action="edit-pet-weight" data-id="${esc(p.id)}"
            data-weight="${esc(p.weight ?? '')}"
            title="Reference weight in grams. Litter-box visits that report no identity go to the pet whose reference is nearest."
            >${p.weight ? esc(Math.round(p.weight)) + ' g' : 'set weight'}</span>
      <span class="pet-devs">${chips || '<span class="mut">no devices assigned</span>'}</span>
      <button class="ghost act" data-action="delete-pet" data-id="${esc(p.id)}">Delete</button>
    </div>
    <div class="tiles">
      ${faces
        .map(
          f => `<div class="tile">
        <img src="${BASE}${esc(f.url)}" alt="Mugshot ${esc(f.id)}">
        <button class="tile-x" title="Remove this photo"
                data-action="delete-face" data-id="${esc(p.id)}" data-face="${esc(f.id)}">✕</button>
      </div>`,
        )
        .join('')}
      ${
        full
          ? ''
          : `<button class="tile tile-add" data-action="crop-open"
                     data-id="${esc(p.id)}" data-name="${esc(p.name)}">
               <span class="plus">+</span><span>Add mugshot</span>
             </button>`
      }
    </div>
    <div class="pet-foot mut">
      ${faces.length} of ${MAX_FACES} mugshots${full ? ' · full' : ''}
    </div>
  </div>`;
}

// Identities the device reports that match no pet. Almost always ids a box
// cached from PetKit's cloud before the takeover — we show them and let the
// user say who they are, rather than guessing (with one pet in the table a
// guess would even look right, and be wrong the day a second animal appears).
function unboundCard(unbound, pets) {
  if (!pets.length) return '';
  return `<div class="card">
    <h3>Unrecognized identities</h3>
    <p class="sub">Your box reported these pet ids, but they are not ours — it is still matching
      against mugshots cached from PetKit's cloud. Bind one to name its past events. Once the
      device picks up your own photos it reports our ids instead and this list empties by itself.</p>
    ${unbound
      .map(
        u => `<div class="unbound">
      <code>#${esc(u.pet_ref)}</code>
      <span class="mut">${esc(u.count)} event${u.count === 1 ? '' : 's'}${
        u.last_ts ? ', last ' + esc(new Date(u.last_ts * 1000).toLocaleString()) : ''
      }</span>
      <span class="grow"></span>
      <select data-role="bind-pet">${pets
        .map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
        .join('')}</select>
      <button class="mini" data-action="bind-pet-ref" data-ref="${esc(u.pet_ref)}">Bind</button>
    </div>`,
      )
      .join('')}
  </div>`;
}

// Rename in place. An input rather than a dialog because this panel has no
// prompt() anywhere and a text field is what it uses for every other edit —
// and because a pet imported from PetKit arrives as "PetKit pet 101392625",
// which is a name nobody wants to keep and the payload gave us nothing better.
onAction('rename-pet', el => {
  const id = el.dataset.id;
  const before = el.textContent;
  const input = document.createElement('input');
  input.className = 'pet-name-edit';
  input.value = before;

  // Enter, Escape and blur can all arrive for one edit — blur fires again when
  // the input is removed — so the first one to finish wins and the rest are
  // no-ops. Without the latch a rename saves twice and Escape saves anyway.
  let done = false;
  const finish = async save => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    input.replaceWith(el);
    if (!save || !name || name === before) return;
    const r = await api('pets/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (r.error) return toast('Error: ' + r.error);
    toast('Renamed');
    loadPets();
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
  el.replaceWith(input);
  input.focus();
  input.select();
});

// The reference weight a litter box with no camera attributes visits by. Same
// in-place pattern as the rename above; an empty field clears the reference,
// which takes the pet out of weight matching altogether.
onAction('edit-pet-weight', el => {
  const id = el.dataset.id;
  const before = el.dataset.weight || '';
  const input = document.createElement('input');
  input.className = 'pet-weight-edit';
  input.type = 'number';
  input.min = '0';
  input.step = '1';
  input.placeholder = 'grams';
  input.value = before ? String(Math.round(Number(before))) : '';

  let done = false;
  const finish = async save => {
    if (done) return;
    done = true;
    const raw = input.value.trim();
    input.replaceWith(el);
    if (!save) return;
    const weight = raw === '' ? null : Number(raw);
    if (weight !== null && !(weight >= 0)) return toast('Weight must be a number of grams');
    const was = before === '' ? null : Math.round(Number(before));
    if (weight === was || (weight !== null && was !== null && Math.round(weight) === was)) return;
    const r = await api('pets/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ weight }),
    });
    if (r.error) return toast('Error: ' + r.error);
    const n = r.reattributed || 0;
    toast('Saved' + (n ? ` · ${n} visit${n === 1 ? '' : 's'} re-attributed` : ''));
    loadPets();
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
  el.replaceWith(input);
  input.focus();
  input.select();
});

onAction('add-pet', () => addPet());
onAction('delete-pet', el => deletePet(el.dataset.id));
onAction('delete-face', el => deleteFace(el.dataset.id, el.dataset.face));

onAction('bind-pet-ref', el =>
  bindPetRef(el.dataset.ref, el.closest('.unbound').querySelector('[data-role=bind-pet]').value),
);

onAction('pets-cloud-import', async el => {
  el.disabled = true;
  toast('Asking PetKit and fetching the photos…');
  const r = await api('pets/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_id: Number(el.dataset.id) }),
  });
  el.disabled = false;
  if (r.error) return toast('Error: ' + r.error);
  const imported = (r.results || []).filter(x => x.outcome === 'imported');
  // Both numbers matter and neither is obvious: a photo can be refused after
  // it downloads (it has to be a real JPEG), and the bound count is history
  // that just got named.
  const photos = imported.reduce((n, x) => n + x.faces_imported, 0);
  const offered = imported.reduce((n, x) => n + x.faces_offered, 0);
  const named = imported.reduce((n, x) => n + x.bound_events, 0);
  toast(
    imported.length
      ? `Imported ${imported.length} pet${imported.length === 1 ? '' : 's'}, ` +
          `${photos} of ${offered} photo${offered === 1 ? '' : 's'}` +
          (named ? `, named ${named} past event${named === 1 ? '' : 's'}` : '')
      : 'Nothing new — PetKit lists no pets this device does not already have',
  );
  loadPets();
});

async function addPet() {
  const name = document.getElementById('newPetName').value.trim();
  if (!name) return toast('Name required');
  const devSel = document.getElementById('newPetDevice');
  const device_ids = devSel.value ? [Number(devSel.value)] : [];
  const weightRaw = document.getElementById('newPetWeight').value.trim();
  const weight = weightRaw === '' ? null : Number(weightRaw);
  const r = await api('pets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, device_ids: device_ids, weight: weight }),
  });
  const n = r.reattributed || 0;
  toast(
    r.pet
      ? 'Pet added' + (n ? ` · ${n} visit${n === 1 ? '' : 's'} attributed by weight` : '')
      : 'Error: ' + (r.error || 'failed'),
  );
  loadPets();
}

async function deletePet(id) {
  if (!confirm('Delete this pet, and every mugshot it has?')) return;
  await api('pets/' + encodeURIComponent(id), { method: 'DELETE' });
  loadPets();
}

async function deleteFace(petId, faceId) {
  // Confirmed like delete-pet: this unlinks a file, and the device keeps
  // matching against it until its next poll, so it is not trivially undone.
  if (!confirm('Remove this mugshot?')) return;
  await api('pets/' + encodeURIComponent(petId) + '/faces/' + encodeURIComponent(faceId), {
    method: 'DELETE',
  });
  loadPets();
}

async function bindPetRef(ref, petId) {
  // Additive: read the pet's current aliases first, or binding a second
  // identity would silently drop the first.
  const cur = (await api('pets/' + encodeURIComponent(petId))).pet || {};
  let ids = [];
  try {
    ids = JSON.parse(cur.device_pet_ids_json || '[]');
  } catch (e) {
    /* an unreadable alias list is replaced, not extended */
  }
  if (!ids.includes(Number(ref))) ids.push(Number(ref));
  const r = await api('pets/' + encodeURIComponent(petId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_pet_ids: ids }),
  });
  toast(
    r.pet
      ? `Bound — ${r.bound_events} past event${r.bound_events === 1 ? '' : 's'} attributed`
      : 'Error: ' + (r.error || 'failed'),
  );
  loadPets();
}

export { loadPets };
