# Copyright (C) 2016-2018  Mikel Artetxe <artetxem@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import embeddings
from cupy_utils import *

import argparse
import collections
import numpy as np
import re
import sys
import time
from lap import lapmod

# Maximum dimensions for the similarity matrix computation in memory
# A MAX_DIM_X * MAX_DIM_Z dimensional matrix will be used
MAX_DIM_X = 10000
MAX_DIM_Z = 10000


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Map the source embeddings into the target embedding space')
    parser.add_argument('src_input', help='the input source embeddings')
    parser.add_argument('trg_input', help='the input target embeddings')
    parser.add_argument('src_output', help='the output source embeddings')
    parser.add_argument('trg_output', help='the output target embeddings')
    parser.add_argument('--encoding', default='utf-8', help='the character encoding for input/output (defaults to utf-8)')
    parser.add_argument('--precision', choices=['fp16', 'fp32', 'fp64'], default='fp64', help='the floating-point precision (defaults to fp64)')
    parser.add_argument('--cuda', action='store_true', help='use cuda (requires cupy)')
    parser.add_argument('--num-words', type=int, help='whether to use only the top n most frequent words for learning embeddings')
    mapping_group = parser.add_argument_group('mapping arguments', 'Basic embedding mapping arguments (EMNLP 2016)')
    mapping_group.add_argument('-d', '--dictionary', default=sys.stdin.fileno(), help='the training dictionary file (defaults to stdin)')
    mapping_group.add_argument('--test-dict', help='the test dictionary file')
    mapping_group.add_argument('--normalize', choices=['unit', 'center', 'unitdim', 'centeremb'], nargs='*', default=[], help='the normalization actions to perform in order')
    mapping_type = mapping_group.add_mutually_exclusive_group()
    mapping_type.add_argument('-c', '--orthogonal', action='store_true', help='use orthogonal constrained mapping')
    mapping_type.add_argument('-u', '--unconstrained', action='store_true', help='use unconstrained mapping')
    self_learning_group = parser.add_argument_group('self-learning arguments', 'Optional arguments for self-learning (ACL 2017)')
    self_learning_group.add_argument('--self_learning', action='store_true', help='enable self-learning')
    self_learning_group.add_argument('--direction', choices=['forward', 'backward', 'union'], default='forward', help='the direction for dictionary induction (defaults to forward)')
    self_learning_group.add_argument('--numerals', action='store_true', help='use latin numerals (i.e. words matching [0-9]+) as the seed dictionary')
    self_learning_group.add_argument('--identical', action='store_true', help='use identical words as training dictionary')
    self_learning_group.add_argument('--threshold', default=0.000001, type=float, help='the convergence threshold (defaults to 0.000001)')
    self_learning_group.add_argument('--validation', default=None, help='a dictionary file for validation at each iteration')
    self_learning_group.add_argument('--log', help='write to a log file in tsv format at each iteration')
    self_learning_group.add_argument('-v', '--verbose', action='store_true', help='write log information to stderr at each iteration')
    self_learning_group.add_argument('--lapmod', action='store_true', help='use the LAPMOD method')
    self_learning_group.add_argument('--lapmod-chunk-size', default=1000, type=int, help='default size of matrix chunks for LAPMOD')
    self_learning_group.add_argument('--lap-repeats', default=1, type=int, help='repeats embeddings to get 2:2, 3:3, etc. alignment')
    self_learning_group.add_argument('--lap-prop', default='1:1', help='specify 1:2 or 2:1 for assymmetric matching')
    self_learning_group.add_argument('--lap-rank', type=int, help='match only the top n most frequent words during matching')
    advanced_group = parser.add_argument_group('advanced mapping arguments', 'Advanced embedding mapping arguments (AAAI 2018)')
    advanced_group.add_argument('--whiten', action='store_true', help='whiten the embeddings')
    advanced_group.add_argument('--src_reweight', type=float, default=0, nargs='?', const=1, help='re-weight the source language embeddings')
    advanced_group.add_argument('--trg_reweight', type=float, default=0, nargs='?', const=1, help='re-weight the target language embeddings')
    advanced_group.add_argument('--src_dewhiten', choices=['src', 'trg'], help='de-whiten the source language embeddings')
    advanced_group.add_argument('--trg_dewhiten', choices=['src', 'trg'], help='de-whiten the target language embeddings')
    advanced_group.add_argument('--dim_reduction', type=int, default=0, help='apply dimensionality reduction')
    args = parser.parse_args()

    # Check command line arguments
    if (args.src_dewhiten is not None or args.trg_dewhiten is not None) and not args.whiten:
        print('ERROR: De-whitening requires whitening first', file=sys.stderr)
        sys.exit(-1)

    if args.verbose:
        print("Info: arguments\n\t" + "\n\t".join(
            ["{}: {}".format(a, v) for a, v in vars(args).items()]),
              file=sys.stderr)

    # Choose the right dtype for the desired precision
    if args.precision == 'fp16':
        dtype = 'float16'
    elif args.precision == 'fp32':
        dtype = 'float32'
    elif args.precision == 'fp64':
        dtype = 'float64'

    # Read input embeddings
    srcfile = open(args.src_input, encoding=args.encoding, errors='surrogateescape')
    trgfile = open(args.trg_input, encoding=args.encoding, errors='surrogateescape')
    src_words, x = embeddings.read(srcfile, dtype=dtype, threshold=200000)
    trg_words, z = embeddings.read(trgfile, dtype=dtype, threshold=200000)

    # NumPy/CuPy management
    if args.cuda:
        if not supports_cupy():
            print('ERROR: Install CuPy for CUDA support', file=sys.stderr)
            sys.exit(-1)
        xp = get_cupy()
        x = xp.asarray(x)
        z = xp.asarray(z)
    else:
        xp = np

    if args.num_words:
        assert args.num_words > 0
        print(f'Restricting source and target words to top {args.num_words} '
              f'words...', file=sys.stderr)
        src_words = src_words[:args.num_words]
        trg_words = trg_words[:args.num_words]
        x = x[:args.num_words]
        z = z[:args.num_words]

    if x.shape[0] > z.shape[0]:
        print('Restricting X to same same shape as Z.')
        src_words = src_words[:z.shape[0]]
        x = x[:z.shape[0]]

    # Build word to index map
    src_word2ind = {word: i for i, word in enumerate(src_words)}
    trg_word2ind = {word: i for i, word in enumerate(trg_words)}

    # Build training dictionary
    src_indices = []
    trg_indices = []
    if args.numerals:
        if args.dictionary != sys.stdin.fileno():
            print('WARNING: Using numerals instead of the training dictionary', file=sys.stderr)
        numeral_regex = re.compile('^[0-9]+$')
        src_numerals = {word for word in src_words if numeral_regex.match(word) is not None}
        trg_numerals = {word for word in trg_words if numeral_regex.match(word) is not None}
        numerals = src_numerals.intersection(trg_numerals)
        for word in numerals:
            src_indices.append(src_word2ind[word])
            trg_indices.append(trg_word2ind[word])
    elif args.identical:
        print('Using identical strings as dictionary...')
        intersect = set(src_words).intersection(set(trg_words))
        print(f'Found {len(intersect)} identical strings.')
        for word in intersect:
            src_indices.append(src_word2ind[word])
            trg_indices.append(trg_word2ind[word])
    else:
        f = open(args.dictionary, encoding=args.encoding, errors='surrogateescape')
        for line in f:
            src, trg = line.split()
            try:
                src_ind = src_word2ind[src]
                trg_ind = trg_word2ind[trg]
                src_indices.append(src_ind)
                trg_indices.append(trg_ind)
            except KeyError:
                print('WARNING: OOV dictionary entry ({0} - {1})'.format(src, trg), file=sys.stderr)

    # Read validation dictionary
    if args.validation is not None:
        f = open(args.validation, encoding=args.encoding, errors='surrogateescape')
        validation = collections.defaultdict(set)
        oov = set()
        vocab = set()
        for line in f:
            src, trg = line.split()
            try:
                src_ind = src_word2ind[src]
                trg_ind = trg_word2ind[trg]
                validation[src_ind].add(trg_ind)
                vocab.add(src)
            except KeyError:
                oov.add(src)
        oov -= vocab  # If one of the translation options is in the vocabulary, then the entry is not an oov
        validation_coverage = len(validation) / (len(validation) + len(oov))

    # Create log file
    if args.log:
        log = open(args.log, mode='w', encoding=args.encoding, errors='surrogateescape')

    # STEP 0: Normalization
    for action in args.normalize:
        if action == 'unit':
            x = embeddings.length_normalize(x)
            z = embeddings.length_normalize(z)
        elif action == 'center':
            x = embeddings.mean_center(x)
            z = embeddings.mean_center(z)
        elif action == 'unitdim':
            x = embeddings.length_normalize_dimensionwise(x)
            z = embeddings.length_normalize_dimensionwise(z)
        elif action == 'centeremb':
            x = embeddings.mean_center_embeddingwise(x)
            z = embeddings.mean_center_embeddingwise(z)

    # Training loop
    prev_objective = objective = -100.
    it = 1
    t = time.time()
    while it == 1 or objective - prev_objective >= args.threshold:
        # Update the embedding mapping
        if args.orthogonal:  # orthogonal mapping solving Procrustes problem
            u, s, vt = xp.linalg.svd(z[trg_indices].T.dot(x[src_indices]))
            w = vt.T.dot(u.T)
            xw = x.dot(w)  # the projected source embeddings
            zw = z
        elif args.unconstrained:  # unconstrained mapping
            x_pseudoinv = xp.linalg.inv(x[src_indices].T.dot(x[src_indices])).dot(x[src_indices].T)
            w = x_pseudoinv.dot(z[trg_indices])
            xw = x.dot(w)
            zw = z
        else:  # advanced mapping
            xw = x
            zw = z

            # STEP 1: Whitening
            def whitening_transformation(m):
                u, s, vt = xp.linalg.svd(m, full_matrices=False)
                return vt.T.dot(xp.diag(1/s)).dot(vt)
            if args.whiten:
                wx1 = whitening_transformation(xw[src_indices])
                wz1 = whitening_transformation(zw[trg_indices])
                xw = xw.dot(wx1)
                zw = zw.dot(wz1)

            # STEP 2: Orthogonal mapping
            wx2, s, wz2_t = xp.linalg.svd(xw[src_indices].T.dot(zw[trg_indices]))
            wz2 = wz2_t.T
            xw = xw.dot(wx2)
            zw = zw.dot(wz2)

            # STEP 3: Re-weighting
            xw *= s**args.src_reweight
            zw *= s**args.trg_reweight

            # STEP 4: De-whitening
            if args.src_dewhiten == 'src':
                xw = xw.dot(wx2.T.dot(xp.linalg.inv(wx1)).dot(wx2))
            elif args.src_dewhiten == 'trg':
                xw = xw.dot(wz2.T.dot(xp.linalg.inv(wz1)).dot(wz2))
            if args.trg_dewhiten == 'src':
                zw = zw.dot(wx2.T.dot(xp.linalg.inv(wx1)).dot(wx2))
            elif args.trg_dewhiten == 'trg':
                zw = zw.dot(wz2.T.dot(xp.linalg.inv(wz1)).dot(wz2))

            # STEP 5: Dimensionality reduction
            if args.dim_reduction > 0:
                xw = xw[:, :args.dim_reduction]
                zw = zw[:, :args.dim_reduction]

        # Self-learning
        if args.self_learning:

            # Update the training dictionary
            best_sim_forward = xp.full(x.shape[0], -100, dtype=dtype)
            src_indices_forward = xp.arange(x.shape[0])
            trg_indices_forward = xp.zeros(x.shape[0], dtype=int)
            best_sim_backward = xp.full(z.shape[0], -100, dtype=dtype)
            src_indices_backward = xp.zeros(z.shape[0], dtype=int)
            trg_indices_backward = xp.arange(z.shape[0])

            if args.lapmod:  # use the LAPMOD algorithm for solving the sparse linear assignment problem
                start = time.time()
                if args.lap_rank is not None:
                    n_rows = args.lap_rank
                    best_sim_forward = xp.full(n_rows, -100, dtype=dtype)
                else:
                    n_rows = xw.shape[0] # number of rows of the assignment cost matrix
                cc = np.empty(n_rows * args.n_similar)  # 1D array of all finite elements of the assignement cost matrix
                kk = np.empty(n_rows * args.n_similar)  # 1D array of the column indices. Must be sorted within one row.
                ii = np.empty((n_rows * args.lap_repeats + 1,), dtype=int)   # 1D array of indices of the row starts in cc.
                ii[0] = 0
                # if each src id should be matched to trg id, then we need to double the source indices
                for i in range(1, n_rows * args.lap_repeats + 1):
                    ii[i] = ii[i - 1] + args.n_similar
                start_time = time.time()
                for i in range(0, n_rows, args.lapmod_chunk_size):
                    j = min(x.shape[0], i + args.lapmod_chunk_size)
                    if args.lap_rank:
                        # only compute the similarity up to the specified rank
                        sim = xw[i:j].dot(zw[:n_rows].T)
                    else:
                        sim = xw[i:j].dot(zw.T)  # get the similarity scores of the source id with all target ids

                    trg_indices = xp.argpartition(sim, -args.n_similar)[:, -args.n_similar:]  # get indices of n largest elements
                    if xp != np:
                        trg_indices = xp.asnumpy(trg_indices)
                    trg_indices.sort()  # sort the target indices

                    trg_indices = trg_indices.flatten()
                    row_indices = np.array([[i] * args.n_similar
                                            for i in range(j-i)]).flatten()
                    sim_scores = sim[row_indices, trg_indices]
                    costs = 1 - sim_scores
                    if xp != np:
                        costs = xp.asnumpy(costs)
                    cc[i * args.n_similar:j * args.n_similar] = costs
                    kk[i * args.n_similar:j * args.n_similar] = trg_indices
                    if i % 10000 == 0 and i > 0:
                        print(f'Processed {i} rows.')
                print(f'Retrieval of ids took {time.time() - start_time}s.')
                if args.lap_repeats > 1:
                    # duplicate costs and target indices
                    new_cc = cc
                    new_kk = kk
                    for i in range(1, args.lap_repeats):
                        new_cc = np.concatenate([new_cc, cc], axis=0)
                        if args.lap_prop == '1:2':
                            # for 1:2, we don't duplicate the target indices
                            new_kk = np.concatenate([new_kk, kk], axis=0)
                        else:
                            # update target indices so that they refer to new columns
                            new_kk = np.concatenate([new_kk, kk + n_rows*i], axis=0)
                    cc = new_cc
                    kk = new_kk
                # trg indices are targets assigned to each row id from 0-(n_rows-1)
                cost, trg_indices, _ = lapmod(n_rows*args.lap_repeats, cc, ii, kk)
                src_indices = np.concatenate([np.arange(n_rows)] * args.lap_repeats, 0)
                src_indices, trg_indices = xp.asarray(src_indices), xp.asarray(trg_indices)
                for i in range(len(src_indices)):
                    src_idx, trg_idx = src_indices[i], trg_indices[i]
                    # we do this if args.lap_repeats > 0 to assign the target
                    # indices in the cost matrix to the correct idx
                    while trg_idx >= n_rows:
                        # if we repeat, we have indices that are > n_rows
                        trg_idx -= n_rows
                        trg_indices[i] = trg_idx
                    best_sim = xw[src_idx].dot(zw[trg_idx].T)
                    best_sim_forward[src_idx] = max(best_sim_forward[src_idx], best_sim)
                print(f'Matching took {time.time() - start}s.')
            else:
                # for efficiency and due to space reasons, look at sub-matrices of
                # size (MAX_DIM_X x MAX_DIM_Z)
                for i in range(0, x.shape[0], MAX_DIM_X):
                    j = min(x.shape[0], i + MAX_DIM_X)
                    if args.verbose:
                        print(f'src ids: {i}-{j}', file=sys.stderr)
                    for k in range(0, z.shape[0], MAX_DIM_Z):
                        l = min(z.shape[0], k + MAX_DIM_Z)
                        # print(f'src ids: {i}-{j}, trg ids: {k}-{l}', file=sys.stderr)
                        sim = xw[i:j].dot(zw[k:l].T)
                        if args.direction in ('forward', 'union'):
                            ind = sim.argmax(axis=1)  # trg indices with max sim for each src id (MAX_DIM_X)
                            val = sim[xp.arange(sim.shape[0]), ind]  # the max sim value for each src id (MAX_DIM_X)
                            ind += k  # add the current position to get the global trg indices
                            mask = (val > best_sim_forward[i:j])  # mask the values if the current value < best sim seen so far for the current src ids
                            best_sim_forward[i:j][mask] = val[mask]  # update the best sim values
                            trg_indices_forward[i:j][mask] = ind[mask]  # update the matched trg indices for the src ids
                        if args.direction in ('backward', 'union'):
                            ind = sim.argmax(axis=0)
                            val = sim[ind, xp.arange(sim.shape[1])]
                            ind += i
                            mask = (val > best_sim_backward[k:l])
                            best_sim_backward[k:l][mask] = val[mask]
                            src_indices_backward[k:l][mask] = ind[mask]
                if args.direction == 'forward':
                    src_indices = src_indices_forward
                    trg_indices = trg_indices_forward
                elif args.direction == 'backward':
                    src_indices = src_indices_backward
                    trg_indices = trg_indices_backward
                elif args.direction == 'union':
                    src_indices = xp.concatenate((src_indices_forward, src_indices_backward))
                    trg_indices = xp.concatenate((trg_indices_forward, trg_indices_backward))

            # Objective function evaluation
            prev_objective = objective
            if args.direction == 'forward':
                objective = xp.mean(best_sim_forward).tolist()
            elif args.direction == 'backward':
                objective = xp.mean(best_sim_backward).tolist()
            elif args.direction == 'union':
                objective = (xp.mean(best_sim_forward) + xp.mean(best_sim_backward)).tolist() / 2

            # Accuracy and similarity evaluation in validation
            if args.validation is not None:
                src = list(validation.keys())
                sim = xw[src].dot(zw.T)  # TODO Assuming that it fits in memory
                nn = asnumpy(sim.argmax(axis=1))
                accuracy = np.mean([1 if nn[i] in validation[src[i]] else 0 for i in range(len(src))])
                similarity = np.mean([max([sim[i, j].tolist() for j in validation[src[i]]]) for i in range(len(src))])

            # Logging
            duration = time.time() - t
            if args.verbose:
                print(file=sys.stderr)
                print('ITERATION {0} ({1:.2f}s)'.format(it, duration), file=sys.stderr)
                print('\t- Objective:        {0:9.4f}%'.format(100 * objective), file=sys.stderr)
                if args.validation is not None:
                    print('\t- Val. similarity:  {0:9.4f}%'.format(100 * similarity), file=sys.stderr)
                    print('\t- Val. accuracy:    {0:9.4f}%'.format(100 * accuracy), file=sys.stderr)
                    print('\t- Val. coverage:    {0:9.4f}%'.format(100 * validation_coverage), file=sys.stderr)
                sys.stderr.flush()
            if args.log is not None:
                val = '{0:.6f}\t{1:.6f}\t{2:.6f}'.format(
                    100 * similarity, 100 * accuracy, 100 * validation_coverage) if args.validation is not None else ''
                print('{0}\t{1:.6f}\t{2}\t{3:.6f}'.format(it, 100 * objective, val, duration), file=log)
                log.flush()

        t = time.time()
        it += 1

        if args.test_dict:
            # save the embeddings for evaluation
            with open(args.src_output, mode='w', encoding=args.encoding, errors='surrogateescape') as srcfile,\
                    open(args.trg_output, mode='w', encoding=args.encoding, errors='surrogateescape') as trgfile:
                embeddings.write(src_words, xw, srcfile)
                embeddings.write(trg_words, zw, trgfile)

            # EVALUATING TRANSLATION
            print('Evaluating translation...')

            # we skip length normalization here

            # Read dictionary and compute coverage
            f = open(args.test_dict, encoding=args.encoding,
                     errors='surrogateescape')
            src2trg = collections.defaultdict(set)
            oov = set()
            vocab = set()
            for line in f:
                src, trg = line.split()
                try:
                    src_ind = src_word2ind[src]
                    trg_ind = trg_word2ind[trg]
                    src2trg[src_ind].add(trg_ind)
                    vocab.add(src)
                except KeyError:
                    oov.add(src)
            src = list(src2trg.keys())
            oov -= vocab  # If one of the translation options is in the vocabulary, then the entry is not an oov
            coverage = len(src2trg) / (len(src2trg) + len(oov))

            BATCH_SIZE = 500

            # Find translations
            translation = collections.defaultdict(int)

            # we just use nearest neighbour for retrieval
            for i in range(0, len(src), BATCH_SIZE):
                j = min(i + BATCH_SIZE, len(src))
                similarities = xw[src[i:j]].dot(zw.T)
                nn = similarities.argmax(axis=1).tolist()
                for k in range(j - i):
                    translation[src[i + k]] = nn[k]

            # Compute accuracy
            accuracy = np.mean(
                [1 if translation[i] in src2trg[i] else 0 for i in src])
            print('Coverage:{0:7.2%}  Accuracy:{1:7.2%}'.format(coverage, accuracy))

    # Write mapped embeddings
    with open(args.src_output, mode='w', encoding=args.encoding, errors='surrogateescape') as srcfile, \
            open(args.trg_output, mode='w', encoding=args.encoding, errors='surrogateescape') as trgfile:
        embeddings.write(src_words, xw, srcfile)
        embeddings.write(trg_words, zw, trgfile)


if __name__ == '__main__':
    main()
