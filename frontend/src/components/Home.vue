<template>
  <div class="layout">
    <Layout>
      <BaseHeader></BaseHeader>
      <Row class="jurvis-main-container">
        <Row type="flex"
             justify="space-around"
             class="info-grid">
          <div class="league-item"
               v-for="item in leagues"
               :key="item.cn"
               :class="[activeInfo.league == item.league_code ? 'active': '']"
               @click="getTeamData(item.league_code)">
            <img :src="item.logo">
            <p>{{item.name}}</p>
            <p>{{item.cn}}</p>
          </div>
        </Row>
        <Row type="flex"
             justify="space-around"
             class="info-grid">
          <div class="empty-team"
               v-if="teams.length == 0">暂无数据</div>
          <div v-else
               class="team-item"
               :class="[activeInfo.home == key || activeInfo.away == key ? 'active':'']"
               v-for="(item, key, index) in teams"
               :key="index">
            <div class="team-choice">
              <div class="choice"
                   @click="getReusltData(key, 0)">主队</div>
              <div class="choice"
                   @click="getReusltData(key, 1)">客队</div>
            </div>
            <img :src="'http://liansai.500.com/static/soccerdata/images/TeamPic/teamsignnew_'+ key + '.png'">
            <p>{{item}}</p>
          </div>
        </Row>
        <Row type="flex"
             justify="space-around"
             class="info-grid">
          <div>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up"
                     v-model="odds.home"
                     :placeholder="activeInfo.home + '主队获胜赔率'"
                     style="width: 120px" />
              <div slot="content">主队获胜赔率</div>
            </Poptip>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up"
                     v-model="odds.draw"
                     :placeholder="activeInfo.home + '主队平球赔率'"
                     style="width: 120px" />
              <div slot="content">{{activeInfo.home}}平球赔率</div>
            </Poptip>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up"
                     v-model="odds.away"
                     :placeholder="activeInfo.home + '主队输球赔率'"
                     style="width: 120px" />
              <div slot="content">{{activeInfo.home}}输球赔率</div>
            </Poptip>
            <Button type="primary"
                    :loading="loading"
                    v-on:click="predictResult"
                    icon="ios-power">
              <span v-if="!loading">Predict!</span>
              <span v-else>Loading...</span>
            </Button>
          </div>
        </Row>
        <Row type="flex"
             justify="center"
             class="info-grid">
          <Row class="prediction">
            <div v-if="predicStatus">
              <p>{{prediction.home}} 与 {{prediction.away}} 的比赛预测结果如下</p>
              <p>{{prediction.home}} 获胜的概率: {{prediction.result[0]}}</p>
              <p>{{prediction.home}} 战平的概率: {{prediction.result[1]}}</p>
              <p>{{prediction.away}} 获胜的概率: {{prediction.result[2]}}</p>
            </div>
            <div v-else>
              <p>输入数据获取预测结果</p>
            </div>
          </Row>
        </Row>
      </Row>
    </Layout>
  </div>
</template>

<script>
import BaseHeader from '@/components/base/BaseHeader';

export default {
  name: 'Home',
  components: {
    BaseHeader,
  },
  data() {
    return {
      msg: 'Welcome to Your Vue.js App',
      activeInfo: {
        league: 'zuqiu-4826',
        home: '主队',
        away: '客队',
      },
      predicStatus: false,
      prediction: {
        home: '',
        away: '',
        result: [],
      },
      odds: {
        home: 0.00,
        away: 0.00,
        draw: 0.00,
      },
      loading: false,
      leagues: [
        {
          name: 'Premier League',
          cn: '英超',
          logo: require('../assets/image/Premier_League.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4826',
        },
        {
          name: 'Li Liga',
          cn: '西甲',
          logo: require('../assets/image/La_Liga.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4913',
        },
        {
          name: 'Serie_A',
          cn: '意甲',
          logo: require('../assets/image/Serie_A.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4919',
        },
        {
          name: 'Bundesliga',
          cn: '德甲',
          logo: require('../assets/image/Bundesliga.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4852',
        },
        {
          name: 'Ligue_1',
          cn: '法甲',
          logo: require('../assets/image/Ligue_1.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4820',
        },
        {
          name: 'Champions_League',
          cn: '欧冠',
          logo: require('../assets/image/Champions_League.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-4838',
        },
        {
          name: 'Chinese_Super_League',
          cn: '中超',
          logo: require('../assets/image/Chinese_Super_League.png'), // eslint-disable-line global-require
          league_code: 'zuqiu-5114',
        },
      ],
      teams: [],
    };
  },
  mounted() {
    this.getTeamData();
  },
  methods: {
    getTeamData(data) {
      const self = this;
      const paramsData = data || 'zuqiu-4826';
      self.activeInfo.league = paramsData;
      this.axios({
        method: 'GET',
        url: 'http://127.0.0.1:5000/get_team_data',
        params: {
          league: paramsData,
          mode: 1,
        },
      }).then((res) => {
        self.requestHandler(res, self.renderTeamData);
      });
    },
    renderTeamData(data) {
      const self = this;
      self.teams = data.data;
    },
    getReusltData(data, type) {
      const self = this;
      if (type === 0) {
        self.activeInfo.home = data;
      } else if (type === 1) {
        self.activeInfo.away = data;
      }
    },
    updatePredictResult(data) {
      const self = this;
      self.prediction = data.data;
      self.predicStatus = !self.predicStatus;
    },
    predictResult() {
      const self = this;
      const h = self.activeInfo.home;
      const a = self.activeInfo.away;
      const win = self.odds.home;
      const lost = self.odds.away;
      const draw = self.odds.draw;
      const l = self.activeInfo.league;
      if (!h || !a || !win || !draw || !lost || !l) {
        return;
      }
      self.axios({
        methods: 'GET',
        url: 'http://127.0.0.1:5000/get_predict',
        params: {
          home: h,
          away: a,
          win,
          lost,
          draw,
          league: l,
        },
      }).then((res) => {
        self.requestHandler(res, self.updatePredictResult);
      });
    },
    requestHandler(res, func) {
      if (res.status === 200 && res.data.errno === 0) {
        func(res.data);
      }
    },
  },
};
</script>

<!-- Add "scoped" attribute to limit CSS to this component only -->
<style scoped lang="less">
.jurvis-main-container {
  padding: 0 20px;
}
.league-item {
  width: 120px;
  height: 80px;
  opacity: 0.6;
  transition: 0.3s;
  img {
    width: 30px;
    height: 30px;
  }
  &:hover {
    cursor: pointer;
    opacity: 1;
    transition: 0.3s;
  }
  &.active {
    opacity: 1;
  }
}
.info-grid {
  padding: 20px 0;
  //border-top: 1px solid #515a6e;
  //border-bottom: 1px solid #515a6e;
}
.empty-team {
  height: 80px;
  line-height: 80px;
}
.team-item {
  position: relative;
  width: 80px;
  height: 60px;
  margin: 10px 0;
  opacity: 0.6;
  img {
    width: 30px;
    height: 30px;
  }
  &:hover {
    cursor: pointer;
    .team-choice {
      display: block;
    }
  }
  &.active {
    opacity: 1;
  }
  .team-choice {
    display: none;
    position: absolute;
    width: 100%;
    height: 100%;
    background-color: #000;
    border-radius: 5px;
    .choice {
      height: 30px;
      line-height: 30px;
      color: #ffffff;
      &:hover {
        color: #2db7f5;
      }
    }
  }
}
</style>
