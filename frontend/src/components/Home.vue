<template>
  <div class="layout">
    <Layout>
      <BaseHeader></BaseHeader>
      <Row class="jurvis-main-container">
        <Row type="flex" justify="space-around" class="info-grid">
          <div
            class="league-item"
            v-for="item in leagues"
            :key="item.cn"
            :class="[active_info.league == item.league_code ? 'active': '']"
            @click="getTeamData(item.league_code)"
          >
            <img :src="item.logo">
            <p>{{item.name}}</p>
            <p>{{item.cn}}</p>
          </div>
        </Row>
        <Row type="flex" justify="space-around" class="info-grid">
          <div class="empty-team" v-if="teams.length == 0">暂无数据</div>
          <div
            v-else
            class="team-item"
            :class="[active_info.team == key ? 'active':'']"
            v-for="(item, key, index) in teams"
            :key="index"
            @click="getReusltData(key)"
          >
            <img
              :src="'http://liansai.500.com/static/soccerdata/images/TeamPic/teamsignnew_'+ key + '.png'"
            >
            <p>{{item}}</p>
          </div>
        </Row>
        <Row type="flex" justify="space-around" class="info-grid">
          <div>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up" placeholder="主队获胜赔率" style="width: 120px"/>
              <div slot="content">主队获胜赔率</div>
            </Poptip>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up" placeholder="主队平球赔率" style="width: 120px"/>
              <div slot="content">主队平球赔率</div>
            </Poptip>
            <Poptip trigger="focus">
              <Input prefix="ios-trending-up" placeholder="主队输球赔率" style="width: 120px"/>
              <div slot="content">主队输球赔率</div>
            </Poptip>
            <Button type="primary" :loading="loading" icon="ios-power" >
              <span v-if="!loading">Predict!</span>
              <span v-else>Loading...</span>
            </Button>
          </div>
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
      active_info: {
        league: 'zuqiu-4826',
        team: '',
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
      self.active_info.league = paramsData;
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
    getReusltData(data) {
      const self = this;
      self.active_info.team = data;
    },
    requestHandler(res, func) {
      if (res.status === 200 && res.data.errno === 0) {
        func(res.data);
      } else {
        console.err('请求失败');
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
  }
  &.active {
    opacity: 1;
  }
}
</style>
