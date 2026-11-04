//! Ground heights of the EQOA worlds, read from your own Frontiers disc image with the
//! game's own zone reader and floor probe (DabDavis's client: `vi::world::World` and a
//! `VIWalkFloorFilter` sphere cast, which passes through players and creatures).
//!
//!     tunaria-heightmap /path/to/frontiers.iso data/heights.bin [--step 8] [--worlds 0,1,2]
//!
//! The file is zlib-compressed, little-endian:
//!
//!     "TRHM", u32 version (1), u32 worlds
//!     per world: u32 world id, u16 name length, name, f32 x0, f32 z0, f32 step,
//!                u32 nx, u32 nz, i16 top[nz][nx] (y x 16; -32768: no ground),
//!                u32 extras, u32 cell[extras], i16 y[extras]
//!
//! `top` is the highest floor at each grid point. Where there is more than one (a
//! building's floors and roof, a bridge, a tree over the grass, a cave under a hill),
//! the lower ones are in the extras, by cell (z * nx + x), in order; a bot stands on
//! the floor nearest under it. Nothing of the disc is copied: only these heights.

use std::io::Write;
use vi::coll::CollSphere;
use vi::fpu::{F, Ieee};
use vi::scene::{Filter, FilterKind};
use vi::vect::Vect3;
use vi::world::World;

/// The world table's files in the order the server numbers worlds (retail's).
const WORLDS: [&str; 7] = ["TUNARIA.ESF", "RATHE.ESF", "ODUS.ESF", "LAVASTM.ESF", "PLANESKY.ESF", "SECRETS.ESF", "UNDERFOOT.ESF"];
const TOP: f32 = 2000.0;
const BOTTOM: f32 = -2000.0;
/// Floors closer than this to the one above are the same floor (its underside).
const SAME_FLOOR: f32 = 2.0;
const MAX_LEVELS: usize = 4;
const NONE: i16 = i16::MIN;

fn floor_under(w: &mut World<Ieee>, x: f32, z: f32, from: f32) -> Option<f32> {
    let mut s = CollSphere::<Ieee>::default();
    s.init(Vect3::of(x, from, z), Vect3::of(x, BOTTOM, z), F::new(0.001));
    let mut f = Filter::new(FilterKind::WalkFloor, -1);
    w.collide_sphere(&mut s, &mut f).then(|| s.hit_point().y.0)
}

/// Every floor at (x, z), highest first.
fn floors(w: &mut World<Ieee>, x: f32, z: f32) -> Vec<f32> {
    let mut out: Vec<f32> = Vec::new();
    let mut from = TOP;
    while out.len() < MAX_LEVELS {
        let Some(y) = floor_under(w, x, z, from) else { break };
        if out.last().is_none_or(|&above| above - y >= SAME_FLOOR) {
            out.push(y);
        }
        from = y - 0.25;
        if from <= BOTTOM {
            break;
        }
    }
    out
}

fn pack(y: f32) -> i16 {
    (y * 16.0).round().clamp(-32767.0, 32767.0) as i16
}

struct Grid {
    id: u32,
    name: String,
    x0: f32,
    z0: f32,
    step: f32,
    nx: u32,
    nz: u32,
    top: Vec<i16>,
    extras: Vec<(u32, i16)>,
}

fn world_grid(iso: &mut iso::Iso, id: u32, name: &str, step: f32) -> Result<Option<Grid>, String> {
    let Ok(bytes) = iso.read(name) else { return Ok(None) };
    let mut lib = esf::Library::new();
    let container = esf::Container::open(bytes).map_err(|e| format!("{name}: {e:?}"))?;
    let file = lib.add(name, container);
    let started = std::time::Instant::now();
    let Some(mut w) = World::<Ieee>::load(&lib, file) else {
        return Err(format!("{name}: not a world file"));
    };
    let bounds: Vec<(f32, f32, f32, f32)> =
        w.zone_bounds.iter().map(|b| (b.min.x.0, b.min.z.0, b.max.x.0, b.max.z.0)).collect();
    if bounds.is_empty() {
        return Ok(None);
    }
    let (mut x0, mut z0, mut x1, mut z1) = (f32::MAX, f32::MAX, f32::MIN, f32::MIN);
    for &(ax, az, bx, bz) in &bounds {
        x0 = x0.min(ax);
        z0 = z0.min(az);
        x1 = x1.max(bx);
        z1 = z1.max(bz);
    }
    x0 = (x0 / step).floor() * step;
    z0 = (z0 / step).floor() * step;
    let nx = ((x1 - x0) / step).ceil() as u32 + 1;
    let nz = ((z1 - z0) / step).ceil() as u32 + 1;
    eprintln!(
        "{name}: {} zones, {nx} x {nz} points every {step} units (loaded in {:.0} s)",
        bounds.len(),
        started.elapsed().as_secs_f32()
    );
    let mut top = vec![NONE; (nx * nz) as usize];
    let mut extras = Vec::new();
    let (mut probed, mut found) = (0u64, 0u64);
    for j in 0..nz {
        let z = z0 + j as f32 * step;
        for i in 0..nx {
            let x = x0 + i as f32 * step;
            if !bounds.iter().any(|&(ax, az, bx, bz)| x >= ax && x <= bx && z >= az && z <= bz) {
                continue;
            }
            probed += 1;
            let levels = floors(&mut w, x, z);
            let cell = j * nx + i;
            if let Some((&first, rest)) = levels.split_first() {
                found += 1;
                top[cell as usize] = pack(first);
                extras.extend(rest.iter().map(|&y| (cell, pack(y))));
            }
        }
        if j % 200 == 0 || j + 1 == nz {
            eprintln!(
                "  {name}: row {}/{nz}, {probed} probed, {found} on ground, {:.0} s",
                j + 1,
                started.elapsed().as_secs_f32()
            );
        }
    }
    Ok(Some(Grid { id, name: name.to_string(), x0, z0, step, nx, nz, top, extras }))
}

fn write(path: &str, grids: &[Grid]) -> std::io::Result<()> {
    let tmp = format!("{path}.tmp");
    let file = std::fs::File::create(&tmp)?;
    let mut z = flate2::write::ZlibEncoder::new(std::io::BufWriter::new(file), flate2::Compression::best());
    z.write_all(b"TRHM")?;
    z.write_all(&1u32.to_le_bytes())?;
    z.write_all(&(grids.len() as u32).to_le_bytes())?;
    for g in grids {
        z.write_all(&g.id.to_le_bytes())?;
        z.write_all(&(g.name.len() as u16).to_le_bytes())?;
        z.write_all(g.name.as_bytes())?;
        for v in [g.x0, g.z0, g.step] {
            z.write_all(&v.to_le_bytes())?;
        }
        z.write_all(&g.nx.to_le_bytes())?;
        z.write_all(&g.nz.to_le_bytes())?;
        for h in &g.top {
            z.write_all(&h.to_le_bytes())?;
        }
        z.write_all(&(g.extras.len() as u32).to_le_bytes())?;
        for (cell, _) in &g.extras {
            z.write_all(&cell.to_le_bytes())?;
        }
        for (_, y) in &g.extras {
            z.write_all(&y.to_le_bytes())?;
        }
    }
    z.finish()?.flush()?;
    std::fs::rename(tmp, path)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let usage = "usage: tunaria-heightmap <frontiers.iso> <out: heights.bin> [--step 8] [--worlds 0,1,...]";
    if args.len() < 3 {
        eprintln!("{usage}");
        std::process::exit(2);
    }
    let mut step = 8.0f32;
    let mut only: Option<Vec<u32>> = None;
    let mut k = 3;
    while k < args.len() {
        match args[k].as_str() {
            "--step" => step = args.get(k + 1).and_then(|s| s.parse().ok()).unwrap_or(step),
            "--worlds" => only = args.get(k + 1).map(|s| s.split(',').filter_map(|w| w.trim().parse().ok()).collect()),
            other => {
                eprintln!("unknown option {other}\n{usage}");
                std::process::exit(2);
            }
        }
        k += 2;
    }
    let mut iso = match iso::Iso::open(&args[1]) {
        Ok(i) => i,
        Err(e) => {
            eprintln!("{}: {e:?}", args[1]);
            std::process::exit(1);
        }
    };
    let mut grids = Vec::new();
    for (id, name) in WORLDS.iter().enumerate() {
        let id = id as u32;
        if only.as_ref().is_some_and(|o| !o.contains(&id)) {
            continue;
        }
        match world_grid(&mut iso, id, name, step) {
            Ok(Some(g)) => grids.push(g),
            Ok(None) => eprintln!("{name}: not on this disc, skipped"),
            Err(e) => eprintln!("{e}"),
        }
    }
    if grids.is_empty() {
        eprintln!("no worlds read: is this the Frontiers disc (SLUS-20744)?");
        std::process::exit(1);
    }
    if let Err(e) = write(&args[2], &grids) {
        eprintln!("{}: {e}", args[2]);
        std::process::exit(1);
    }
    eprintln!("wrote {} world(s) to {}", grids.len(), args[2]);
}
