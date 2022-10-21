import { Suspense } from 'react';
import dynamic from 'next/dynamic';
// Util

// Client Components
import GroupBy from './GroupBy.client';
import MatrixSelect from './MatrixSelect.client';
import SortBy from './SortBy.client';
import FilterExpr from './FilterExpr.client';
import DialogueModal from '../Modals/DialogueModalContainer.client';
import CustomizeColumnsModal from './CustomizeColumns.client';
import { Pages } from '@material-ui/icons';
import Paging from './Paging.client';
import Skeletons from '../skeletons';
import SelectRow from './SelectRow.client';
import RefreshButton from './RefreshButton.client';


const SortButton = () => (
    <div className="button-outline">
        <img src="/sort_icon.png" /> <span>Sort</span>
    </div>
);

const GroupButton = () => (
    <div className="button-outline">
        <img src="/group_placeholder.png" /> <span>Group by</span>
    </div>
);

const SelectButton = () => (
    <div className="button-outline">
        <img src="/columns_placeholder.png" /> <span>Columns</span>
    </div>
);

const KangasButton = () => (
    <div className="button-outline">
        <img src="/favicon.png" />
        <span>Kangas</span>
    </div>
);

const StatusText = ({ status }) => {
    const items = Object.keys(status.data).map(item => (
	    <li className="kangas-list-item">
	      <span className="kangas-item">{item}</span>: <span className="kangas-value">{status.data[item]}</span>
	    </li>));

    return(
	    <div>
	       <h1 className="kangas-title">&#129432; Kangas DataGrid</h1>
	       <hr/>
               <p className="kangas-text">© 2022 Kangas DataGrid Development Team</p>
	       <div className="kangas-text">
	         {items}
	       </div>
            <p className="kangas-text">For help, contributions, examples, and discussions, see: <a href="https://www.github.com/comet-ml/kangas" target="_blank">github.com/comet-ml/kangas</a></p>
	    <p className="kangas-text">Consider giving us a github <span className="kangas-item">&#10029;</span>!</p>
	   </div>
	  );
};

const SettingsBarServer = ({ query, matrices, columns, options, status }) => {
    return (
        <div id="settings-bar">
            <div id="nav-bar-1">
                <DialogueModal fullScreen={false} toggleElement={<KangasButton />}>
                    <StatusText status={status} />
                </DialogueModal>
                <div id="matrix-select" className="select-row">
                    <MatrixSelect query={query} options={matrices} />
                    <RefreshButton query={query} />
                </div>
            </div>
            <div id="nav-bar">
                <SelectRow columns={columns} query={query} options={options} />
            </div>
        </div>
    );
};

export default SettingsBarServer;
