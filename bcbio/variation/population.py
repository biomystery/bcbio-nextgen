"""Provide infrastructure to allow exploration of variations within populations.

Uses the gemini framework (https://github.com/arq5x/gemini) to build SQLite
database of variations for query and evaluation.
"""
import collections
import os
import subprocess

from bcbio import utils
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import config_utils
from bcbio.provenance.diagnostics import log_cmd
from bcbio.variation import effects, genotype

def prep_gemini_db(fnames, call_id, data):
    """Prepare a gemini database from VCF inputs prepared with snpEff.
    """
    out_dir = utils.safe_makedir(os.path.join(data["dirs"]["work"], "gemini"))
    gemini_db = os.path.join(out_dir, "-".join(call_id) + ".db")
    if not utils.file_exists(gemini_db):
        if len(fnames) > 1:
            gemini_vcf = "%s.vcf" % os.path.splitext(gemini_db)[0]
            gemini_vcf = genotype.combine_variant_files(fnames, gemini_vcf, data["sam_ref"],
                                                        data["config"])
        else:
            gemini_vcf = fnames[0]
        with file_transaction(gemini_db) as tx_gemini_db:
            gemini = config_utils.get_program("gemini", data["config"])
            num_cores = data["config"]["algorithm"].get("num_cores", 1)
            cmd = "{gemini} load -v {gemini_vcf} -t snpEff --cores {num_cores} {tx_gemini_db}"
            cmd = cmd.format(**locals())
            log_cmd("Create gemini database for %s" % str(call_id), data["info"]["provenance"], cmd)
            subprocess.check_call(cmd, shell=True)
    return [[call_id, gemini_db]]

def _do_db_build(samples):
    """Confirm we should build a gemini database: need gemini + human samples.
    """
    config = samples[0][0]["config"]
    gemini = config_utils.get_program("gemini", config)
    try:
        subprocess.check_call(["gemini", "-h"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except:
        return False
    genomes = set()
    for data in [x[0] for x in samples]:
        genomes.add(data["genome_build"])
    if len(genomes) > 1:
        return False
    else:
        return effects.SNPEFF_GENOME_REMAP[genomes.pop()].is_human

def _group_by_batches(samples):
    """Group data items into batches, providing details to retrieve results.
    """
    batch_groups = collections.defaultdict(list)
    singles = []
    out_retrieve = []
    for data in [x[0] for x in samples]:
        batch = data.get("metadata", {}).get("batch")
        if batch:
            out_retrieve.append((batch, data))
        else:
            out_retrieve.append((data["name"][-1], data))
        for vrn in data["variants"]:
            if batch:
                batch_groups[(batch, vrn["variantcaller"])].append((vrn["vrn_file"], data))
            else:
                singles.append((data["name"][-1], vrn["variantcaller"], data, vrn["vrn_file"]))
    return batch_groups, singles, out_retrieve

def prep_db_parallel(samples, parallel_fn):
    """Prepares gemini databases in parallel, handling jointly called populations.
    """
    if len(samples) > 0 and not _do_db_build(samples):
        return samples
    batch_groups, singles, out_retrieve = _group_by_batches(samples)
    to_process = []
    for (name, caller), info in batch_groups.iteritems():
        fnames = [x[0] for x in info]
        data = info[0][1]
        to_process.append([fnames, (name, caller), data])
    for name, caller, data, fname in singles:
        to_process.append([[fname], (name, caller), data])
    output = parallel_fn("prep_gemini_db", to_process)
    out_fetch = {}
    for batch_id, out_file in output:
        out_fetch[batch_id] = out_file
    out = []
    for batch_name, data in out_retrieve:
        out_variants = []
        for vrn in data["variants"]:
            vrn["pop_db"] = out_fetch[(batch_name, vrn["variantcaller"])]
            out_variants.append(vrn)
        data["variants"] = out_variants
        out.append([data])
    return out