""" Script for running notebook with SeismicGeometry tests."""
import glob
import json
import os
import pprint
from datetime import date
from ..batchflow.utils_notebook import run_notebook


# Constants
DATESTAMP = date.today().strftime("%Y-%m-%d")
DROP_EXTRA_FILES = True
SHOW_TEST_ERROR_INFO = True
TESTS_SCRIPTS_DIR = os.getenv("TESTS_SCRIPTS_DIR", os.path.dirname(os.path.realpath(__file__))+'/')
TEST_FOLDER = os.path.join(TESTS_SCRIPTS_DIR, 'notebooks/geometry_test_files/')
SHOW_MESSAGE = True

def test_geometry(capsys):
    """ Run SeismicGeometry test notebook."""
    # Delete old test notebook results
    previous_output_files = glob.glob(os.path.join(TESTS_SCRIPTS_DIR, 'notebooks/geometry_test_out_*.ipynb'))
    for file in previous_output_files:
        os.remove(file)

    out_path_ipynb = os.path.join(TESTS_SCRIPTS_DIR, f'notebooks/geometry_test_out_{DATESTAMP}.ipynb')

    # Tests execution
    exec_info = run_notebook(
        path=os.path.join(TESTS_SCRIPTS_DIR, 'notebooks/geometry_test.ipynb'),
        nb_kwargs={
            'TEST_FOLDER': TEST_FOLDER,
            'NOTEBOOKS_FOLDER': os.path.join(TESTS_SCRIPTS_DIR, 'notebooks/'),
            'DATESTAMP': DATESTAMP,
            'DROP_EXTRA_FILES': DROP_EXTRA_FILES,
            'SHOW_TEST_ERROR_INFO': SHOW_TEST_ERROR_INFO
        },
        insert_pos=0,
        out_path_ipynb=out_path_ipynb,
        display_links=False
    )

    with capsys.disabled():
        # Extract and drop message
        if SHOW_MESSAGE:
            message_path = glob.glob(os.path.join(TEST_FOLDER, 'message*.txt'))[-1]
            with open(message_path, "r") as infile:
                for line in infile.readlines():
                    print(line)

        if DROP_EXTRA_FILES:
            os.remove(message_path)

        # Extract timings data
        timings_path = glob.glob(os.path.join(TEST_FOLDER, 'timings_*.json'))
        timings_path = sorted(timings_path)[-1]
        with open(timings_path, "r") as infile:
            timings = json.load(infile)
            pp = pprint.PrettyPrinter()
            pp.pprint(timings)

        # Output message and extra file deleting
        if timings['state']=='OK':
            print('Tests were executed successfully.\n')

            if DROP_EXTRA_FILES:
                os.remove(out_path_ipynb)
        else:
            print(f'An ERROR occured in cell number {exec_info}:\n{out_path_ipynb}\n')
            assert False
