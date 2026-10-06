//! The store's CLI (`kkernel`) with the `lion` pack force-linked. Select it at runtime with
//! `KHIVE_PACKS=kg,comm,lion` or `[runtime].packs`; registration is the pack crate's own
//! `inventory::submit!`, so the `use` below only makes the linker keep it.
use khive_pack_lion as _;

fn main() -> anyhow::Result<()> {
    kkernel::cli::cli_main()
}
