import random
from sys import stderr, argv
import pysam
from pysam import CSOFT_CLIP, CHARD_CLIP, CDEL, CINS, CDIFF, CMATCH
import time
import datetime
import os
from dysgu.map_set_utils import Py_BasicIntervalTree, is_overlapping
from dysgu.graph import AlignmentsSA
import logging
from collections import defaultdict
from enum import Enum
from dysgu.scikitbio._ssw_wrapper import StripedSmithWaterman
import zlib
import edlib


random.seed(42)


def parse_SM_name(infile, path, ignore_RG, warn=True):
    if ignore_RG:
        return os.path.splitext(os.path.basename(path))[0]
    if "RG" in infile.header:
        rg = infile.header["RG"]
        if "SM" in rg[0]:
            if len(rg) > 1:
                sms = set([i["SM"] for i in rg])
                if len(sms) > 1:
                    if warn:
                        logging.warning(
                            "Warning: more than one sample in @RG, using first sample (SM) for output: {}".format(
                                rg[0]["SM"]))
            sample_name = rg[0]["SM"]
        else:
            sample_name = os.path.splitext(os.path.basename(path))[0]
            if warn:
                logging.warning(
                    "Warning: no SM tag in @RG (read-group), using input file name as sample name: {}".format(
                        sample_name))
    else:
        sample_name = os.path.splitext(os.path.basename(path))[0]
        if warn:
            logging.warning("Warning: no @RG, using input file name as sample name: {}".format(sample_name))
    return sample_name



def get_bam_paths(args):
    pths = args['normal_bams']
    if args["random_bam_sample"] > 0:
        n = min(args["random_bam_sample"], len(pths))
        pths = random.choices(pths, k=n)
        logging.info(f'Random bam sample: {pths}')
    return pths


def load_bams(args, pths, vcf_sample, warn=True):
    bams = {}
    for pth in pths:
        if pth.endswith('.cram'):
            if args['reference'] is None:
                logging.exception('Need --reference to work with .cram files')
                quit()
            b = pysam.AlignmentFile(pth, reference_filename=args['reference'], threads=args['procs'])
        else:
            b = pysam.AlignmentFile(pth, reference_filename=args['reference'])
        sample_name = parse_SM_name(b, pth, args['ignore_read_groups'], warn)
        if sample_name != vcf_sample:
            bams[sample_name] = b
    return bams


def load_samples(args, pths):
    vcf = pysam.VariantFile(args['input_vcf'])
    vcf_sample = list(vcf.header.samples)
    if len(vcf_sample) != 1:
        if args['target_sample'] == "":
            logging.exception(f"Input vcf {args['input_vcf']} has {len(vcf_sample)} samples: {vcf_sample}, but expected only 1. Use --target-sample to select sample to filter")
            quit()
        else:
            logging.info(f"Multi-sample input file with samples: {vcf_sample}")
            vcf_sample = args["target_sample"]
            if not vcf_sample in vcf_sample:
                raise ValueError(f"--target-sample was not in vcf header samples: {vcf_sample}")
    else:
        vcf_sample = vcf_sample[0]
    bams = load_bams(args, pths, vcf_sample)
    logging.info(f"Filtering {args['input_vcf']} against bams {list(bams.keys())}")
    if len(bams) == 0:
        logging.warning("No target bams in input list. Target bams must have a unique SM/name from input_vcf sample name")
    if args['normal_vcf']:
        normal_vcfs = [pysam.VariantFile(i) for i in args['normal_vcf']]
    else:
        normal_vcfs = None
    return vcf_sample, vcf, bams, normal_vcfs


def output_vcf(args, template_vcf):
    hdr = template_vcf.header
    input_command = ' '.join(argv)
    hdr.add_line('##FILTER=<ID=normal,Description="SV has read support in a normal vcf or alignment file">')
    hdr.add_line('##FILTER=<ID=lowSupport,Description="Not enough supporting evidence / too many nuisance reads">')
    hdr.add_line(f'##command="{input_command}"')
    return pysam.VariantFile("-" if not args['svs_out'] else args['svs_out'], "w", header=hdr)


def infer_paired_end(bams):
    is_paired_end = {}
    for k, b in bams.items():
        is_paired_end[k] = False
        for count, aln in enumerate(b.fetch(until_eof=True)):
            if aln.flag & 162 or aln.next_reference_start > 0:
                is_paired_end[k] = True
                break
            if count > 100:
                break
    return is_paired_end


def vcf_chroms_to_tids(r, infile):
    if infile:
        chrom = infile.gettid(r.chrom)
        # try and switch between "chr" representation
        if chrom == -1:
            if "chr" in r.chrom:
                chrom = infile.gettid(r.chrom[3:])
            else:
                chrom = infile.gettid("chr" + r.chrom)
        if "CHROM2" in r.info:
            chrom2_info = r.info["CHROM2"]
            chrom2 = infile.gettid(chrom2_info)
            if chrom2 == -1:
                if "chr" in chrom2_info:
                    chrom2 = infile.gettid(chrom2_info[3:])
                else:
                    chrom2 = infile.gettid("chr" + chrom2_info)
        else:
            chrom2 = chrom
        if chrom == -1 or chrom2 == -1:
            logging.warning(f"Chromosome from vcf record not found in bam file header CHROM={r.chrom}, POS={r.start}, CHROM2={chrom2}, END={r.stop}")
    else:
        chrom = r.chrom
        if "CHROM2" in r.info:
            chrom2 = r.info["CHROM2"]
        else:
            chrom2 = chrom

    return chrom, chrom2


def get_posB(r):
    if r.info['SVTYPE'] == 'TRA' and "CHR2_POS" in r.info:
        return r.info['CHR2_POS']
    return r.stop


def get_sv_type(r, chrom, chrom2):
    svt = r.info["SVTYPE"]
    if chrom != chrom2 and svt == "BND":
        svt = "TRA"
    if "DUP" in svt and svt != "DUP":
        svt = "DUP"
    return svt


def make_interval_tree(args, infile, sample_name, normal_vcfs):
    if not normal_vcfs:
        return None
    intervals = defaultdict(lambda: Py_BasicIntervalTree())
    ignored = defaultdict(int)
    added = 0
    for normal_vcf in normal_vcfs:
        distance = args['interval_size']
        for idx, r in enumerate(normal_vcf):
            chrom, chrom2 = vcf_chroms_to_tids(r, infile)
            if 'GT' in r.format:
                in_sample = False
                in_any = False
                for k in list(r.samples):
                    if k == sample_name and r.samples[k]['GT'] != (0,):
                        in_sample = True
                        break
                    elif r.samples[k]['GT'] != (0,):
                        in_any = True
                if in_sample or not in_any:
                    continue
            svt = r.info["SVTYPE"]
            if chrom != chrom2 and svt in {"INS", "DEL", "DUP", "INV"}:
                raise ValueError(f"CHROM2 must equal chrom for SVTYPE {svt}, (chrom {r.chrom}, pos {r.start})")
            if chrom != chrom2 and svt == "BND":
                svt = "TRA"
            if "DUP" in svt and svt != "DUP":
                svt = "DUP"
            if svt not in {"INS", "DEL", "DUP", "INV", "TRA", "BND"}:
                ignored[svt] += 1
                continue
            start = r.start  # note pysam converts to zero-based index like bam
            stop = r.stop
            if chrom == chrom2 and stop < start:
                start = r.stop - 1
                stop = r.start - 1
            if chrom == chrom2 and abs(stop - start) <= distance:
                intervals[chrom].add(min(start, stop) - distance, max(start, stop) + distance, idx)
            else:
                posB = get_posB(r)
                intervals[chrom].add(start - distance, start + distance, idx)
                intervals[chrom2].add(posB - distance, posB + distance, idx)
            added += 1
    for tree in intervals.values():
        tree.index()
    if ignored:
        logging.warning(f'Ignored SV types: {ignored}')
    if added == 0:
        logging.warning(f'No SVs in --normal-vcf? Check that sample name in --normal-vcf doesnt match the input vcf/bam')
    else:
        logging.info(f"Loaded {added} SVs from normal-vcf")
    return intervals


def span_position_distance(x1, x2, y1, y2, pos_threshold=20, span_threshold=0.3):
    span1 = max(1, abs(x2 - x1))
    center1 = (x1 + x2) / 2
    span2 = max(1, abs(y2 - y1))
    center2 = (y1 + y2) / 2
    position_distance = max(1, abs(center1 - center2))
    max_span = max(span1, span2)
    span_distance = abs(span1 - span2) / max_span
    if (position_distance / max_span) < pos_threshold and span_distance < span_threshold:
        return 1
    return 0


class SeqType(Enum):
    WITHIN_READ = 0
    LEFT_CLIP = 1
    RIGHT_CLIP = 2
    ANY_CLIP = 3


def get_left_clip(r):
    ct = r.cigartuples
    if ct[0][0] == 4 and r.query_sequence:
        return r.query_sequence[:ct[0][1]]
    else:
        return ""


def get_right_clip(r):
    ct = r.cigartuples
    if ct[-1][0] == 4 and r.query_sequence:
        return r.query_sequence[-ct[-1][1]:]
    else:
        return ""


def get_contig_clipped_bases(cont, extend_by_ratio=2):
    if not cont:
        return None, None
    left = ""
    right = ""
    if cont[0].islower():
        i = 1
        while cont[i].islower():
            i += 1
        left = cont[:(extend_by_ratio*i)-1]
    if cont[-1].islower():
        i = -1
        while cont[i].islower():
            i -= 1
        i = max(0, len(cont) + (extend_by_ratio*i))
        right = cont[i:]
    return left, right


class BreakSeqs:
    def __init__(self, r):
        # grab the candidate soft-clips from any contigs, keep longest
        self.left_clip_A = ""
        self.right_clip_A = ""
        self.left_clip_B = ""
        self.right_clip_B = ""
        self.alt_seq = ""
        if r.alts[0][0] != "<":
            self.alt_seq = r.alts[0].upper()
        if "CONTIGA" in r.info and r.info["CONTIGA"]:
            left_clip, right_clip = get_contig_clipped_bases(r.info["CONTIGA"])
            if left_clip:
                self.left_clip_A = left_clip.upper()
            if right_clip:
                self.right_clip_A = right_clip.upper()
            if not left_clip and not right_clip and r.info["WR"] > 0:
                self.alt_seq = r.info["CONTIGA"]
        if "CONTIGB" in r.info and r.info["CONTIGB"]:
            left_clip, right_clip = get_contig_clipped_bases(r.info["CONTIGB"])
            if left_clip:
                self.left_clip_B = left_clip.upper()
            if right_clip:
                self.right_clip_B = right_clip.upper()
        self.any_seqs = any((self.left_clip_A, self.left_clip_B, self.right_clip_A, self.right_clip_B, self.alt_seq))


def positions(posA, posB):
    if posA < posB:
        return posA, posB
    else:
        return posB, posA


def iterate_bams(bams, chrom, start, chrom2, end, pad, bam_is_paired_end):
    is_intra = chrom == chrom2
    for name, bam in bams.items():
        is_pe = bam_is_paired_end[name]
        seen = set([])
        if chrom != chrom2 or abs(end - start) > pad:
            for align in bam.fetch(chrom, max(0, start - pad), start + pad):
                if align.flag & 1540 or not align.cigartuples:
                    continue
                if is_intra:
                    seen.add((align.qname, align.flag, align.pos))
                yield is_pe, align
            for align in bam.fetch(chrom2, max(0, end - pad),  end + pad):
                if align.flag & 1540 or not align.cigartuples:
                    continue
                if is_intra and (align.qname, align.flag, align.pos) in seen:
                    continue
                yield is_pe, align
        else:
            for align in bam.fetch(chrom, max(0, start - pad),  end + pad):
                if align.flag & 1540 or not align.cigartuples:
                    continue
                yield is_pe, align


def iterate_bams_single_region(bams, chrom, start, pad, bam_is_paired_end):
    for name, bam in bams.items():
        is_pe = bam_is_paired_end[name]
        for align in bam.fetch(chrom, max(0, start - pad),  start + pad):
            if align.flag & 1540 or not align.cigartuples:
                continue
            yield is_pe, align


def ends_close(posA, posB, a_posA, a_posB):
    if abs(posA - a_posA) < 1000 and abs(posB - a_posB) < 1000:
        return True


def matching_supplementary(aln, infile, posA, posB):
    if aln.has_tag('SA'):
        all_aligns = AlignmentsSA(aln, infile.gettid)
        all_aligns.connect_alignments(aln)
        if len(all_aligns.query_aligns) < 2:
            return False
        for j in all_aligns.join_result:
            if j.chrom == j.chrom2:
                a_posA, a_posB = positions(j.event_pos, j.pos2)
            else:
                a_posA = j.event_pos
                a_posB = j.pos2
            spd = span_position_distance(posA, posB, a_posA, a_posB, span_threshold=0.5)
            if spd and ends_close(posA, posB, a_posA, a_posB):
                return True
    if aln.flag & 2048:
        ct = aln.cigartuples
        if ct[0][0] == 5 or ct[0][0] == 4:
            if abs(aln.pos - posA) < 5:
                return True
            if abs(aln.pos - posB) < 5:
                return True
        if ct[-1][0] == 5 or ct[-1][0] == 4:
            if abs(aln.reference_end - posA) < 5:
                return True
            if abs(aln.reference_end - posB) < 5:
                return True
    return False


def matching_gap(posA, posB, r, svtype, is_insertion, svlen, pos_threshold=100, span_threshold=0.1):
    ct = r.cigartuples
    pos = r.pos
    min_length = svlen * 0.2  # 0.25
    max_distance = svlen * 10
    ins_like = svtype == "DUP" or svtype == "INV"
    for k, l in ct:
        if k == CHARD_CLIP or k == CSOFT_CLIP:
            continue
        if k == CMATCH or k == CDIFF:
            pos += l
            continue
        if is_insertion and k == CDEL:
            pos += l
            continue
        elif not is_insertion and k == CINS:
            if l > 250 and ins_like:
                return True
            elif svlen * 2 < l and (abs(posA - pos) < max_distance or (abs(posB - pos + l) < max_distance)):
                return True
            continue
        end = pos + l
        if l > min_length and abs(posA - pos) < max_distance:
            if is_insertion:
                if span_position_distance(posA, posA + svlen, pos, end, pos_threshold, span_threshold):
                    return True
            elif span_position_distance(posA, posB, pos, end, pos_threshold, span_threshold):
                return True
        if k == CDEL:
            pos += l
        if pos > posB + svlen:
            break
    return False


def pos_covered(posA, r):
    ct = r.cigartuples
    pos = r.pos
    for k, l in ct:
        end = pos + l
        if k == CHARD_CLIP or k == CSOFT_CLIP:
            continue
        if k == CDEL:
            pos += l
            continue
        if k == CMATCH or k == CDIFF:
            if pos <= posA <= end:
                return True
            pos += l
        if k == CINS:
            if pos <= posA <= end:
                return True
        if end > posA:
            break
    return False


def good_step_translocation(record, sample):
    if record.samples[sample]['ICN'] > 0 and (record.samples[sample]['OCN'] / record.samples[sample]['ICN'] > 1.1):
        if record.samples[sample]['COV'] > 0 and record.info["SU"] > record.samples[sample]['COV']*0.15:
            return True
    return False


def matching_ins_translocation(posA, r):
    ct = r.cigartuples
    pos = r.pos
    max_distance = 500
    for k, l in ct:
        if k == CHARD_CLIP or k == CSOFT_CLIP:
            continue
        if k == CMATCH or k == CDIFF:
            pos += l
            continue
        if k == CDEL:
            pos += l
            continue
        if k == CINS:
            if l > 250 and abs(posA - pos) < max_distance:
                return True
            continue
        if pos > posA + max_distance:
            break
    return False


def clip_position_matches(cigar_block, clip_length, event_pos, clip_pos, distance):
    if (cigar_block[0] == CSOFT_CLIP or cigar_block[0] == CHARD_CLIP) and cigar_block[1] >= clip_length:
        if abs(event_pos - clip_pos) < distance:
            return True
    return False


def cache_nearby_soft_clip(posA, posB, align, join_type, svtype, cached, distance=50, clip_length=3):
    if len(cached) > 100:
        return False
    ct = align.cigartuples
    if svtype == "TRA" or svtype == "BND" or svtype == "INV" or svtype == "DUP":  # respect join orientation
        if join_type[0] == "3":
            if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
                cached.append((SeqType.RIGHT_CLIP, align, "A"))
                return True
        else:
            if clip_position_matches(ct[0], clip_length, posA, align.pos, distance):
                cached.append((SeqType.LEFT_CLIP, align, "A"))
                return True
        if join_type[-1] == "3":
            if clip_position_matches(ct[-1], clip_length, posB, align.reference_end, distance):
                cached.append((SeqType.RIGHT_CLIP, align, "B"))
                return True
        else:
            if clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
                cached.append((SeqType.LEFT_CLIP, align, "B"))
                return True
    elif svtype == "DEL":  # Ignore join orientation, can be wrong due to re-mapping, use position instead
        if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
            cached.append((SeqType.RIGHT_CLIP, align, "A"))
            return True
        if clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
            cached.append((SeqType.LEFT_CLIP, align, "B"))
            return True
    elif svtype == "INS":  # check all clips
        value = False
        if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
            value = True
            cached.append((SeqType.RIGHT_CLIP, align, "A"))
        if clip_position_matches(ct[0], clip_length, posA, align.pos, distance):
            value = True
            cached.append((SeqType.LEFT_CLIP, align, "A"))
        if clip_position_matches(ct[-1], clip_length, posB, align.reference_end, distance):
            value = True
            cached.append((SeqType.RIGHT_CLIP, align, "B"))
        if clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
            value = True
            cached.append((SeqType.LEFT_CLIP, align, "B"))
        return value
    return False


def any_nearby_soft_clip(posA, posB, align, join_type, svtype, distance=30, clip_length=3):
    ct = align.cigartuples
    if svtype == "BND" or svtype == "INV" or svtype == "DUP":  # respect join orientation
        if join_type[0] == "3":
            if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
                return True
        elif clip_position_matches(ct[0], clip_length, posA, align.pos, distance):
            return True
        if join_type[-1] == "3":
            if clip_position_matches(ct[-1], clip_length, posB, align.reference_end, distance):
                return True
        elif clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
            return True
    elif svtype == "DEL":  # Ignore join orientation, can be wrong due to re-mapping, use position instead
        if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
            return True
        if clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
            return True
    elif svtype == "INS" or svtype == "TRA":  # check all clips
        if clip_position_matches(ct[-1], clip_length, posA, align.reference_end, distance):
            return True
        if clip_position_matches(ct[-1], clip_length, posB, align.reference_end, distance):
            return True
        if clip_position_matches(ct[0], clip_length, posB, align.pos, distance):
            return True
        if clip_position_matches(ct[0], clip_length, posA, align.pos, distance):
            return True
    return False


def has_clip(r):
    if r.cigartuples[0][0] == CSOFT_CLIP or r.cigartuples[-1][0] == CSOFT_CLIP or r.cigartuples[0][0] == CHARD_CLIP or r.cigartuples[-1][0] == CHARD_CLIP:
        return True
    return False


def clip_align_matches(seq1, seq2, clip_side, paired_end):
    if not seq1 or not seq2:
        return False
    min_seq = min(len(seq1), len(seq2))
    if min_seq < 8:
        if min_seq == len(seq2):
            return False
        if clip_side == SeqType.RIGHT_CLIP:
            seq2_clipped = seq2[int(len(seq2) / 2) + (1 if len(seq2) % 2 == 0 else 0):]
            if seq2_clipped.startswith(seq1):
                return True
        else:
            seq2_clipped = seq2[:int(len(seq2) / 2)]
            if seq2_clipped.endswith(seq1):
                return True
        return False
    if len(seq1) < 16000 and len(seq2) < 16000 and min_seq < 2000:
        aln = StripedSmithWaterman(seq1, match_score=2, mismatch_score=-3, gap_open_penalty=4, gap_extend_penalty=1)
        a = aln(seq2)
        score = a.optimal_alignment_score
        pct = score / (min_seq * 2)
        if pct > 0.7:
            return True
    else:
        el = edlib.align(seq1, seq2, mode="HW", task="locations")
        if el and el['locations']:
            pct = el['locations'][0][1] - el['locations'][0][0] / min_seq
            if pct > 0.7:
                return True
    if paired_end:
        seq1_e = seq1.encode("ascii")
        seq2_e = seq2.encode("ascii")
        s1_s2 = len(zlib.compress(bytes(seq1_e + seq2_e)))
        compress_ratio = s1_s2 / (len(seq1) + len(seq2))
        if compress_ratio < 0.5:
            return True
    return False


def matching_soft_clips(r, reads_with_nearby_soft_clips, pe):
    break_seqs = BreakSeqs(r)
    if not break_seqs.any_seqs:
        return False
    for clip_side, align, target_side in reads_with_nearby_soft_clips:
        checked = False
        ct = align.cigartuples
        if target_side == "A":
            if clip_side == SeqType.LEFT_CLIP and break_seqs.left_clip_A:
                checked = True
                if clip_align_matches(get_left_clip(align), break_seqs.left_clip_A, SeqType.LEFT_CLIP, pe) or (pe and ct[0][0] == CHARD_CLIP):
                    return True
            elif clip_side == SeqType.RIGHT_CLIP and break_seqs.right_clip_A:
                checked = True
                if clip_align_matches(get_right_clip(align), break_seqs.right_clip_A, SeqType.RIGHT_CLIP, pe) or (pe and ct[-1][0] == CHARD_CLIP):
                    return True
        else:
            if clip_side == SeqType.LEFT_CLIP and break_seqs.left_clip_B:
                checked = True
                if clip_align_matches(get_left_clip(align), break_seqs.left_clip_B, SeqType.LEFT_CLIP, pe) or (pe and ct[0][0] == CHARD_CLIP):
                    return True
            elif clip_side == SeqType.RIGHT_CLIP and break_seqs.right_clip_B:
                checked = True
                if clip_align_matches(get_right_clip(align), break_seqs.right_clip_B, SeqType.RIGHT_CLIP, pe) or (pe and ct[-1][0] == CHARD_CLIP):
                    return True
        if not checked and break_seqs.alt_seq:
            if clip_align_matches(get_left_clip(align), break_seqs.alt_seq, SeqType.LEFT_CLIP, pe):
                return True
            if clip_align_matches(get_right_clip(align), break_seqs.alt_seq, SeqType.RIGHT_CLIP, pe):
                return True
    return False


def has_low_support(r, sample, support_fraction):
    cov = r.samples[sample]['COV']
    min_support = round(1.5 + support_fraction * cov)
    if r.info['SU'] < min_support:
        return True
    return False


def has_low_WR_support(r, sample, support_fraction):
    sf = support_fraction / 2
    cov = r.samples[sample]['COV']
    min_support = min(4, round(1.5 + sf * cov))
    if 0 < r.info['WR'] < min_support:
        return True
    return False


def too_many_clipped_reads(r, clipped_reads):
    cov = r.samples[r.samples.keys()[0]]['COV']
    max_nearby_clipped_reads = round(3 + 0.025 * cov)
    if clipped_reads > max_nearby_clipped_reads:
        return True
    return False


def process_translocation(r, chromB, posB, bams, infile, bam_is_paired_end, pad, keep_all, sample):
    ct = r.info["CT"]
    chromA = r.chrom
    posA = r.pos
    current_start = r.pos - pad
    current_end = r.pos + pad
    next_start = posB - pad
    next_end = posB + pad
    cached = []
    nearby_soft_clips = 0
    has_contig = 'CONTIG' in r.info or 'CONTIG2' in r.info or r.alts[0][0] != "<"
    is_paired_end = False
    good_step = good_step_translocation(r, sample)
    for is_paired_end, aln in iterate_bams_single_region(bams, chromA, posA, pad, bam_is_paired_end):
        if is_paired_end:
            distance = 50
            clip_length = 3
            if aln.flag & 12 or (aln.flag & 2 and not has_clip(aln)):
                continue
            if chromB == infile.get_reference_name(aln.rnext) and is_overlapping(next_start, next_end, aln.pnext - pad, aln.pnext + pad):
                if keep_all:
                    r.filter.add("normal")
                return False
            if matching_supplementary(aln, infile, posA, posB):
                if keep_all:
                    r.filter.add("normal")
                return False
            if has_contig:
                cache_nearby_soft_clip(posA, posB, aln, ct, "TRA", cached, distance, clip_length)
        else:
            distance = 500
            clip_length = 50
            if aln.flag & 4 or not has_clip(aln):
                continue
            if matching_supplementary(aln, infile, posA, posB):
                if keep_all:
                    r.filter.add("normal")
                return False
            if not good_step and matching_ins_translocation(posA, aln):
                if keep_all:
                    r.filter.add("normal")
                return False
            if has_contig:
                cache_nearby_soft_clip(posA, posB, aln, ct, "TRA", cached, distance, clip_length)
                if any_nearby_soft_clip(posA, posB, aln, ct, "TRA", 30, clip_length=50):
                    nearby_soft_clips += 1
            elif any_nearby_soft_clip(posA, posB, aln, ct, "TRA", 30, clip_length=250):
                if keep_all:
                    r.filter.add("lowSupport")
                return False

    for is_paired_end, aln in iterate_bams_single_region(bams, chromB, posB, pad, bam_is_paired_end):
        if is_paired_end:
            distance = 50
            clip_length = 3
            if aln.flag & 12 or (aln.flag & 2 and not has_clip(aln)):
                continue
            if r.chrom == infile.get_reference_name(aln.rnext) and is_overlapping(current_start, current_end, aln.pnext - pad, aln.pnext + pad):
                if keep_all:
                    r.filter.add("normal")
                return False
            if matching_supplementary(aln, infile, posB, posA):
                if keep_all:
                    r.filter.add("normal")
                return False
            if has_contig:
                cache_nearby_soft_clip(r.pos, posB, aln, ct, "TRA", cached, distance, clip_length)
        else:
            distance = 500
            clip_length = 50
            if aln.flag & 4 or not has_clip(aln):
                continue
            if matching_supplementary(aln, infile, posB, posA):
                if keep_all:
                    r.filter.add("normal")
                return False
            if not good_step and matching_ins_translocation(posB, aln):
                if keep_all:
                    r.filter.add("normal")
                return False
            if has_contig:
                cache_nearby_soft_clip(posA, posB, aln, ct, "TRA", cached, distance, clip_length)
                if any_nearby_soft_clip(r.pos, posB, aln, ct, "TRA", 30, clip_length=50):
                    nearby_soft_clips += 1
            elif any_nearby_soft_clip(posA, posB, aln, ct, "TRA", 30, clip_length=250):
                if keep_all:
                    r.filter.add("lowSupport")
                return False

    if not is_paired_end and too_many_clipped_reads(r, nearby_soft_clips):
        if keep_all:
            r.filter.add("lowSupport")
        return False
    if cached and matching_soft_clips(r, cached, is_paired_end):
        if keep_all:
            r.filter.add("normal")
        return False
    return True


def process_intra(r, posB, bams, infile, bam_is_paired_end, support_fraction, pad, sample, keep_all):
    svlen = r.info["SVLEN"]
    svtype = r.info["SVTYPE"]
    is_insertion = svtype == "INS"
    posA = r.pos
    ct = r.info["CT"]
    cached = []
    covered = 0
    nearby_soft_clips = 0
    is_paired_end = False
    for is_paired_end, aln in iterate_bams(bams, r.chrom, posA, r.chrom, posB, pad, bam_is_paired_end):
        if is_paired_end:
            a_posA = min(aln.pos, aln.pnext)
            a_posB = max(aln.pos, aln.pnext)
            if aln.flag & 4:
                continue
            if aln.rname != aln.rnext:
                if matching_supplementary(aln, infile, posA, posB):
                    if keep_all:
                        r.filter.add("normal")
                    return False
                cache_nearby_soft_clip(posA, posB, aln, ct, svtype, cached)
                continue
            if abs(a_posA - posA) > pad or abs(a_posB - posB) > pad:
                continue
            if not is_insertion and not aln.flag & 10:  # proper pair, mate unmapped
                if span_position_distance(posA, posB, a_posA, a_posB, pos_threshold=20, span_threshold=0.5):
                    if keep_all:
                        r.filter.add("normal")
                    return False
            if not is_insertion and matching_supplementary(aln, infile, posA, posB):
                if keep_all:
                    r.filter.add("normal")
                return False
            if svlen < 80 and matching_gap(posA, posB, aln, svtype, is_insertion, svlen):
                if keep_all:
                    r.filter.add("normal")
                return False
            cache_nearby_soft_clip(posA, posB, aln, ct, svtype, cached, distance=50, clip_length=3)

        else:
            if matching_gap(posA, posB, aln, svtype, is_insertion, svlen, pos_threshold=30, span_threshold=0.7):
                if keep_all:
                    r.filter.add("normal")
                return False
            if matching_supplementary(aln, infile, posA, posB):
                if keep_all:
                    r.filter.add("normal")
                return False
            # if is_overlapping(posA, posA + 1, aln.pos, aln.reference_end):
            #     covered += 1
            if pos_covered(posA, aln):
                covered += 1
            if any_nearby_soft_clip(posA, posB, aln, ct, svtype, 30, clip_length=50):
                nearby_soft_clips += 1
            cache_nearby_soft_clip(posA, posB, aln, ct, svtype, cached, distance=min(500, svlen * 0.5), clip_length=50)

    if not is_paired_end and not covered:
        if keep_all:
            r.filter.add("lowSupport")
        return False
    if not is_paired_end and has_low_WR_support(r, sample, support_fraction):
        if keep_all:
            r.filter.add("lowSupport")
        return False
    if not is_paired_end and too_many_clipped_reads(r, nearby_soft_clips):
        if keep_all:
            r.filter.add("lowSupport")
        return False
    if cached and matching_soft_clips(r, cached, is_paired_end):
        if keep_all:
            r.filter.add("normal")
        return False
    return True


def run_filtering(args):
    t0 = time.time()
    pths = get_bam_paths(args)
    sample_name, vcf, bams, normal_vcfs = load_samples(args, pths)
    out_vcf = output_vcf(args, vcf)
    infile = list(bams.values())[0] if bams else None
    intervals = make_interval_tree(args, infile, sample_name, normal_vcfs)
    bam_is_paired_end = infer_paired_end(bams)
    bams = load_bams(args, pths, sample_name, warn=False)
    pad = args["interval_size"]
    keep_all = args["keep_all"]
    min_prob = args['min_prob']
    pass_prob = args['pass_prob']
    support_fraction = args['support_fraction']
    filter_results = defaultdict(int)
    written = 0
    for idx, r in enumerate(vcf):
        # if r.id != "20690":
        #     continue
        r.filter.clear()
        if min_prob != 0 and 'PROB' in r.samples[sample_name] and r.samples[sample_name]['PROB'] < min_prob:
            filter_results['dropped, lowProb'] += 1
            if keep_all:
                r.filter.add("lowProb")
                out_vcf.write(r)
            continue
        if has_low_support(r, sample_name, support_fraction):
            filter_results['dropped, low support'] += 1
            if keep_all:
                r.filter.add("lowSupport")
                out_vcf.write(r)
            continue
        chrom_tid, chrom2_tid = vcf_chroms_to_tids(r, infile)
        posB = get_posB(r)
        if chrom_tid == -1:
            logging.exception(f'Chromosome name {r.chrom} not found in bam file header')
        if intervals:
            if chrom_tid in intervals:
                posA_overlaps = set(intervals[chrom_tid].allOverlappingIntervals(r.pos, r.pos+1))
                if chrom2_tid in intervals:
                    posB_overlaps = set(intervals[chrom2_tid].allOverlappingIntervals(posB, posB + 1))
                    if posA_overlaps.intersection(posB_overlaps):
                        filter_results['dropped, normal SV overlap'] += 1
                        if keep_all:
                            r.filter.add("normal")
                            out_vcf.write(r)
                        continue
        if not bams:
            if pass_prob != 1 and 'PROB' in r.samples[sample_name] and r.samples[sample_name]['PROB'] >= pass_prob:
                r.filter.add("PASS")
            out_vcf.write(r)
            written += 1
            continue
        sv_type = get_sv_type(r, chrom_tid, chrom2_tid)
        if sv_type == "TRA" or sv_type == "BND":
            good = process_translocation(r, r.info["CHR2"], posB, bams, infile, bam_is_paired_end, pad=pad, keep_all=keep_all, sample=sample_name)
        else:
            good = process_intra(r, posB, bams, infile, bam_is_paired_end, support_fraction, pad=pad, sample=sample_name, keep_all=keep_all)
        if good:
            if pass_prob != 1 and 'PROB' in r.samples[sample_name] and r.samples[sample_name]['PROB'] >= pass_prob:
                r.filter.add("PASS")
            else:
                r.filter.add("lowProb")
            out_vcf.write(r)
            written += 1
        else:
            if keep_all:
                out_vcf.write(r)
            filter_results['dropped, normal read support'] += 1
    out_vcf.close()
    logging.info(f'Filter results: {dict(sorted(filter_results.items()))}')
    logging.info("dysgu filter-normal {} complete, n={}, h:m:s, {}".format(args['input_vcf'],
                                                                           written,
                                                                           str(datetime.timedelta(seconds=int(time.time() - t0))),
                                                                           time.time() - t0))

